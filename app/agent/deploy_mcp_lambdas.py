# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deploy MCP server Lambda functions using container images.

Uses Docker container images to avoid Lambda's 250MB unzipped size limit.
Each MCP server is packaged as a container image, pushed to ECR, and
deployed as a Lambda function with the `run-mcp-servers-with-aws-lambda`
wrapper that manages the MCP lifecycle per invocation:

  1. Start the stdio MCP server as a child process
  2. Initialize the MCP server (protocol handshake)
  3. Forward the incoming MCP JSON-RPC request
  4. Return the server's response
  5. Shut down the MCP server child process

Prerequisites:
  - AWS credentials configured
  - Docker installed and running
  - pip install boto3 python-dotenv
  - policies/ directory with IAM policy JSON files

Usage:
  python deploy_mcp_lambdas.py
  python deploy_mcp_lambdas.py --cleanup
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)


# ── Rollback Stack ───────────────────────────────────────────────────────────


@dataclass
class RollbackStack:
    """Tracks created resources and rolls them back in reverse order on failure."""

    _actions: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def push(self, resource_name: str, rollback_fn: Callable[[], None]) -> None:
        """Register a rollback action for a successfully created resource."""
        self._actions.append((resource_name, rollback_fn))

    def rollback(self) -> None:
        """Execute all rollback actions in reverse order (LIFO)."""
        if not self._actions:
            logger.info("  No resources to roll back.")
            return

        logger.info("  Rolling back %d resource(s)...", len(self._actions))
        for resource_name, rollback_fn in reversed(self._actions):
            try:
                logger.info("  Deleting: %s", resource_name)
                rollback_fn()
                logger.info("  ✓ Deleted: %s", resource_name)
            except Exception as e:
                logger.warning("  ✗ Failed to delete %s: %s", resource_name, e)

        self._actions.clear()

# ── Configuration ────────────────────────────────────────────────────────────

REGION: str = os.getenv("AWS_REGION", "us-east-1")
PROJECT_NAME: str = "rdsAgenticWAR"
LAMBDA_TIMEOUT: int = 300
LAMBDA_MEMORY: int = 512
LAMBDA_ROLE_NAME: str = f"McpLambdaExecRole-{PROJECT_NAME}"
ECR_REPO_NAME: str = f"{PROJECT_NAME.lower()}-mcp-servers"
CONFIG_OUTPUT: str = "mcp_lambdas_config.json"

# MCP servers to deploy.
# Format: (function_suffix, pip_package, server_command, description)
# When installed via pip, these packages register console_scripts entry points.
# The command is the package name itself (e.g., "awslabs.postgres-mcp-server").
MCP_SERVERS: list[tuple[str, str, list[str], str]] = [
    (
        "AwsApi",
        "awslabs.aws-api-mcp-server",
        ["awslabs.aws-api-mcp-server"],
        "AWS API access for RDS, Aurora, EC2, IAM, and other service calls",
    ),
    (
        "Postgres",
        "awslabs.postgres-mcp-server",
        ["awslabs.postgres-mcp-server"],
        "PostgreSQL and Aurora PostgreSQL database queries and schema inspection",
    ),
    (
        "CloudWatch",
        "awslabs.cloudwatch-mcp-server",
        ["awslabs.cloudwatch-mcp-server"],
        "CloudWatch metrics, alarms, and log group analysis",
    ),
    (
        "BillingCost",
        "awslabs.billing-cost-management-mcp-server",
        ["awslabs.billing-cost-management-mcp-server"],
        "AWS Cost Explorer, reservations, and savings plans data",
    ),
    (
        "AwsDocs",
        "awslabs.aws-documentation-mcp-server",
        ["awslabs.aws-documentation-mcp-server"],
        "AWS documentation search and retrieval",
    ),
]

# ── Lambda handler template ──────────────────────────────────────────────────

HANDLER_TEMPLATE = '''"""Auto-generated Lambda handler for {server_name} MCP server."""
import os
from mcp.client.stdio import StdioServerParameters
from mcp_lambda import BedrockAgentCoreGatewayTargetHandler, StdioServerAdapterRequestHandler


def _build_server_params():
    """Build server params at invocation time to get fresh credentials."""
    return StdioServerParameters(
        command="{command}",
        args={args},
        env={{
            "PATH": os.environ.get("PATH", "") + ":/var/lang/bin:/var/task/bin",
            "HOME": "/tmp",
            "AWS_REGION": os.environ.get("AWS_REGION", "{region}"),
            "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "{region}"),
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            "AWS_SESSION_TOKEN": os.environ.get("AWS_SESSION_TOKEN", ""),
            "FASTMCP_LOG_LEVEL": os.environ.get("FASTMCP_LOG_LEVEL", "ERROR"),
        }},
    )


# Build handler at module load (cold start) — credentials are available here
server_params = _build_server_params()
request_handler = StdioServerAdapterRequestHandler(server_params=server_params)
gateway_handler = BedrockAgentCoreGatewayTargetHandler(request_handler=request_handler)


def lambda_handler(event, context):
    """AWS Lambda entry point for AgentCore Gateway Lambda target."""
    return gateway_handler.handle(event, context)
'''

# ── Dockerfile template ──────────────────────────────────────────────────────

DOCKERFILE_TEMPLATE = '''FROM public.ecr.aws/lambda/python:3.12

# Install the MCP server package and the Lambda wrapper
RUN pip install --no-cache-dir \\
    run-mcp-servers-with-aws-lambda \\
    {pip_package}

# Copy the handler
COPY lambda_function.py ${{LAMBDA_TASK_ROOT}}/

CMD ["lambda_function.lambda_handler"]
'''


# ── Helper functions ─────────────────────────────────────────────────────────


def _get_account_id() -> str:
    """Retrieve the current AWS account ID."""
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


def _rollback_role(iam_client: Any) -> None:
    """Delete the Lambda execution role (detach policies first)."""
    try:
        attached = iam_client.list_attached_role_policies(RoleName=LAMBDA_ROLE_NAME)
        for policy in attached.get("AttachedPolicies", []):
            iam_client.detach_role_policy(
                RoleName=LAMBDA_ROLE_NAME, PolicyArn=policy["PolicyArn"]
            )
    except ClientError:
        pass
    iam_client.delete_role(RoleName=LAMBDA_ROLE_NAME)


def _create_lambda_execution_role(iam_client: Any, account_id: str) -> str:
    """Create a shared execution role for all MCP server Lambda functions."""
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    })

    try:
        iam_client.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description=f"Execution role for {PROJECT_NAME} MCP server Lambdas",
            Tags=[
                {"Key": "Project", "Value": PROJECT_NAME},
                {"Key": "ManagedBy", "Value": "deploy_mcp_lambdas.py"},
            ],
        )
        iam_client.attach_role_policy(
            RoleName=LAMBDA_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        logger.info("  Created execution role: %s", LAMBDA_ROLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.info("  Execution role already exists: %s", LAMBDA_ROLE_NAME)
        else:
            raise

    # Create and attach project-specific policies from policies/ directory
    policies_dir = Path(__file__).parent / "policies"
    policy_files = [
        (f"{PROJECT_NAME}-RdsAccess", "rds-access.json"),
        (f"{PROJECT_NAME}-CostAccess", "cost-access.json"),
        (f"{PROJECT_NAME}-ObservabilityAccess", "observability-access.json"),
    ]

    for policy_name, filename in policy_files:
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        policy_path = policies_dir / filename

        if policy_path.exists():
            try:
                iam_client.create_policy(
                    PolicyName=policy_name,
                    PolicyDocument=policy_path.read_text(encoding="utf-8"),
                    Description=f"{PROJECT_NAME} Lambda policy: {filename}",
                    Tags=[
                        {"Key": "Project", "Value": PROJECT_NAME},
                        {"Key": "ManagedBy", "Value": "deploy_mcp_lambdas.py"},
                    ],
                )
                logger.info("  Created policy: %s", policy_name)
            except ClientError as e:
                if e.response["Error"]["Code"] == "EntityAlreadyExists":
                    logger.info("  Policy already exists: %s", policy_name)
                else:
                    logger.warning("  Could not create %s: %s", policy_name, e)
                    continue

        try:
            iam_client.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn=policy_arn)
            logger.info("  Attached: %s", policy_name)
        except ClientError as e:
            logger.warning("  Could not attach %s: %s", policy_name, e)

    role_arn = f"arn:aws:iam::{account_id}:role/{LAMBDA_ROLE_NAME}"
    logger.info("  Role ARN: %s", role_arn)
    logger.info("  Waiting 10s for IAM propagation...")
    time.sleep(10)
    return role_arn


def _ensure_ecr_repo(account_id: str) -> str:
    """Create ECR repository if it doesn't exist. Returns the repository URI."""
    ecr_client = boto3.client("ecr", region_name=REGION)

    try:
        response = ecr_client.describe_repositories(repositoryNames=[ECR_REPO_NAME])
        repo_uri = response["repositories"][0]["repositoryUri"]
        logger.info("  ECR repo exists: %s", ECR_REPO_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryNotFoundException":
            response = ecr_client.create_repository(
                repositoryName=ECR_REPO_NAME,
                imageScanningConfiguration={"scanOnPush": True},
                tags=[
                    {"Key": "Project", "Value": PROJECT_NAME},
                    {"Key": "ManagedBy", "Value": "deploy_mcp_lambdas.py"},
                ],
            )
            repo_uri = response["repository"]["repositoryUri"]
            logger.info("  Created ECR repo: %s", ECR_REPO_NAME)
        else:
            raise

    return repo_uri


def _docker_login(account_id: str) -> None:
    """Authenticate Docker to ECR."""
    ecr_client = boto3.client("ecr", region_name=REGION)
    token_response = ecr_client.get_authorization_token()
    auth_data = token_response["authorizationData"][0]
    registry = auth_data["proxyEndpoint"]

    token = base64.b64decode(auth_data["authorizationToken"]).decode("utf-8")
    username, password = token.split(":")

    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        input=password.encode(),
        check=True,
        capture_output=True,
    )
    logger.info("  Docker authenticated to ECR")


def _build_and_push_image(
    server_name: str,
    pip_package: str,
    server_command: list[str],
    repo_uri: str,
) -> str:
    """Build a Docker image for the MCP server and push to ECR.

    Returns:
        The full image URI with tag.
    """
    build_dir = Path(tempfile.mkdtemp(prefix=f"mcp-lambda-{server_name}-"))

    try:
        # Write Dockerfile
        (build_dir / "Dockerfile").write_text(
            DOCKERFILE_TEMPLATE.format(pip_package=pip_package), encoding="utf-8"
        )

        # Write handler
        handler_code = HANDLER_TEMPLATE.format(
            server_name=server_name,
            region=REGION,
            command=server_command[0],
            args=json.dumps(server_command[1:]),
        )
        (build_dir / "lambda_function.py").write_text(handler_code, encoding="utf-8")

        # Build image
        image_tag = f"{repo_uri}:{server_name.lower()}"
        logger.info("  Building Docker image: %s", image_tag)

        result = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64",
             "--provenance=false", "--output=type=docker",
             "-t", image_tag, "."],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed:\n{result.stderr}")

        # Push to ECR
        logger.info("  Pushing to ECR...")
        result = subprocess.run(
            ["docker", "push", image_tag],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker push failed:\n{result.stderr}")

        logger.info("  ✓ Pushed: %s", image_tag)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    return image_tag


def _deploy_lambda(
    lambda_client: Any,
    function_name: str,
    role_arn: str,
    image_uri: str,
    description: str,
) -> dict[str, str]:
    """Create or update a Lambda function using a container image.

    Returns:
        Dict with function_name, function_arn, and function_url.
    """
    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Role=role_arn,
            Code={"ImageUri": image_uri},
            PackageType="Image",
            Description=description,
            Timeout=LAMBDA_TIMEOUT,
            MemorySize=LAMBDA_MEMORY,
            Environment={
                "Variables": {
                    "FASTMCP_LOG_LEVEL": "ERROR",
                }
            },
            Tags={
                "Project": PROJECT_NAME,
                "ManagedBy": "deploy_mcp_lambdas.py",
            },
        )
        function_arn = response["FunctionArn"]
        logger.info("  Created function: %s", function_name)

        waiter = lambda_client.get_waiter("function_active_v2")
        waiter.wait(FunctionName=function_name)

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            response = lambda_client.update_function_code(
                FunctionName=function_name,
                ImageUri=image_uri,
            )
            function_arn = response["FunctionArn"]
            logger.info("  Updated function: %s", function_name)

            waiter = lambda_client.get_waiter("function_updated_v2")
            waiter.wait(FunctionName=function_name)
        else:
            raise

    # Create/get Function URL
    try:
        url_response = lambda_client.create_function_url_config(
            FunctionName=function_name,
            AuthType="AWS_IAM",
        )
        function_url = url_response["FunctionUrl"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            url_response = lambda_client.get_function_url_config(
                FunctionName=function_name
            )
            function_url = url_response["FunctionUrl"]
        else:
            raise

    logger.info("  Function URL: %s", function_url)

    return {
        "function_name": function_name,
        "function_arn": function_arn,
        "function_url": function_url,
    }


# ── Main deploy ──────────────────────────────────────────────────────────────


def deploy_mcp_lambdas() -> dict[str, Any]:
    """Build and deploy all MCP server Lambda functions as container images.

    On failure, automatically rolls back all created resources.
    """
    logger.info("=" * 70)
    logger.info("RDS Agentic WAR — MCP Server Lambda Deployment (Container)")
    logger.info("  Region:   %s", REGION)
    logger.info("  Servers:  %d", len(MCP_SERVERS))
    logger.info("=" * 70)

    iam_client = boto3.client("iam")
    lambda_client = boto3.client("lambda", region_name=REGION)
    ecr_client = boto3.client("ecr", region_name=REGION)
    account_id = _get_account_id()

    stack = RollbackStack()

    try:
        # Step 1: Create shared execution role
        logger.info("\n[Step 1/5] Creating Lambda execution role...")
        role_arn = _create_lambda_execution_role(iam_client, account_id)
        stack.push(f"IAM Role: {LAMBDA_ROLE_NAME}", lambda: _rollback_role(iam_client))

        # Step 2: Setup ECR and Docker auth
        logger.info("\n[Step 2/5] Setting up ECR repository...")
        repo_uri = _ensure_ecr_repo(account_id)
        stack.push(f"ECR Repo: {ECR_REPO_NAME}", lambda: ecr_client.delete_repository(
            repositoryName=ECR_REPO_NAME, force=True
        ))
        _docker_login(account_id)

        # Step 3: Build, push, and deploy each MCP server
        logger.info("\n[Step 3/5] Building and deploying MCP server containers...")
        deployed: list[dict[str, str]] = []

        for suffix, pip_package, server_command, description in MCP_SERVERS:
            function_name = f"{PROJECT_NAME}-mcp-{suffix}"
            logger.info("\n  ── %s ──", function_name)

            image_uri = _build_and_push_image(suffix, pip_package, server_command, repo_uri)
            result = _deploy_lambda(lambda_client, function_name, role_arn, image_uri, description)
            result["image_uri"] = image_uri
            result["status"] = "deployed"
            deployed.append(result)

            # Register rollback for this Lambda
            def _rollback_lambda(fn=function_name):
                try:
                    lambda_client.delete_function_url_config(FunctionName=fn)
                except ClientError:
                    pass
                lambda_client.delete_function(FunctionName=fn)

            stack.push(f"Lambda: {function_name}", _rollback_lambda)

        # Step 4: Extract tool schemas by running MCP servers locally via Docker
        logger.info("\n[Step 4/5] Extracting tool schemas from MCP servers...")
        for entry, (suffix, pip_package, server_command, description) in zip(deployed, MCP_SERVERS):
            if entry.get("status") != "deployed":
                continue
            func_name = entry["function_name"]
            logger.info("  Extracting tools from: %s", func_name)
            try:
                image_uri = entry.get("image_uri", "")

                # Write extraction script to a temp file
                extract_dir = Path(tempfile.mkdtemp(prefix="mcp-extract-"))
                extract_script = extract_dir / "extract_tools.py"
                extract_script.write_text(
                    "import json, asyncio\n"
                    "from mcp.client.stdio import StdioServerParameters, stdio_client\n"
                    "from mcp import ClientSession\n"
                    "\n"
                    "async def main():\n"
                    "    params = StdioServerParameters(\n"
                    f"        command='{server_command[0]}',\n"
                    f"        args={server_command[1:]},\n"
                    "        env={'FASTMCP_LOG_LEVEL': 'ERROR'},\n"
                    "    )\n"
                    "    async with stdio_client(params) as (read, write):\n"
                    "        async with ClientSession(read, write) as session:\n"
                    "            await session.initialize()\n"
                    "            tools = await session.list_tools()\n"
                    "            print(json.dumps([t.model_dump() for t in tools.tools]))\n"
                    "\n"
                    "asyncio.run(main())\n",
                    encoding="utf-8",
                )

                result = subprocess.run(
                    [
                        "docker", "run", "--rm", "--platform", "linux/amd64",
                        "-v", f"{extract_script}:/tmp/extract_tools.py:ro",
                        "--entrypoint", "python",
                        image_uri,
                        "/tmp/extract_tools.py",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                shutil.rmtree(extract_dir)

                if result.returncode == 0 and result.stdout.strip():
                    # Take the last line (in case of startup warnings on stdout)
                    last_line = result.stdout.strip().split("\n")[-1]
                    tools = json.loads(last_line)
                    entry["tool_schema"] = tools
                    logger.info("  ✓ %s: %d tools extracted", func_name, len(tools))
                else:
                    logger.warning("  ⚠️  %s: extraction failed: %s",
                                   func_name, (result.stderr or result.stdout)[:300])
                    entry["tool_schema"] = []
            except Exception as e:
                logger.warning("  ⚠️  %s: Could not extract tools: %s", func_name, e)
                entry["tool_schema"] = []

        # Step 5: Save configuration
        logger.info("\n[Step 5/5] Saving configuration...")
        config: dict[str, Any] = {
            "region": REGION,
            "execution_role_arn": role_arn,
            "ecr_repo_uri": repo_uri,
            "lambdas": deployed,
        }

        config_path = Path(__file__).parent / CONFIG_OUTPUT
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    except Exception as e:
        logger.error("=" * 70)
        logger.error("DEPLOYMENT FAILED: %s", e)
        logger.error("Initiating rollback...")
        logger.error("=" * 70)
        stack.rollback()
        logger.error("=" * 70)
        logger.error("ROLLBACK COMPLETE. Fix the issue and re-run.")
        logger.error("=" * 70)
        sys.exit(1)

    # Summary (only reached on success)
    successful = [d for d in deployed if d.get("status") == "deployed"]

    logger.info("\n" + "=" * 70)
    logger.info("CREATE_COMPLETE — Deployment successful!")
    logger.info("  Deployed:  %d/%d", len(successful), len(MCP_SERVERS))
    logger.info("  Config:    %s", config_path)
    logger.info("")
    logger.info("Function ARNs (use with setup_gateway.py):")
    for d in successful:
        logger.info("  %s: %s", d["function_name"], d.get("function_arn", "N/A"))
    logger.info("=" * 70)

    return config


# ── Cleanup ──────────────────────────────────────────────────────────────────


def cleanup_mcp_lambdas() -> None:
    """Delete all deployed MCP server Lambda functions, ECR images, role, and policies."""
    config_path = Path(__file__).parent / CONFIG_OUTPUT
    if not config_path.exists():
        logger.error("Config not found: %s", config_path)
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    lambda_client = boto3.client("lambda", region_name=config.get("region", REGION))
    iam_client = boto3.client("iam")
    ecr_client = boto3.client("ecr", region_name=config.get("region", REGION))

    logger.info("Cleaning up MCP server Lambdas...")

    # 1. Delete Lambda functions
    for entry in config.get("lambdas", []):
        func_name = entry.get("function_name")
        if not func_name:
            continue
        try:
            try:
                lambda_client.delete_function_url_config(FunctionName=func_name)
            except ClientError:
                pass
            lambda_client.delete_function(FunctionName=func_name)
            logger.info("  ✓ Deleted Lambda: %s", func_name)
        except ClientError as e:
            logger.warning("  ✗ %s: %s", func_name, e)

    # 2. Delete ECR repository and all images
    try:
        ecr_client.delete_repository(repositoryName=ECR_REPO_NAME, force=True)
        logger.info("  ✓ Deleted ECR repo: %s", ECR_REPO_NAME)
    except ClientError as e:
        logger.warning("  ✗ ECR repo: %s", e)

    # 3. Delete execution role
    try:
        attached = iam_client.list_attached_role_policies(RoleName=LAMBDA_ROLE_NAME)
        for policy in attached.get("AttachedPolicies", []):
            iam_client.detach_role_policy(
                RoleName=LAMBDA_ROLE_NAME, PolicyArn=policy["PolicyArn"]
            )
        iam_client.delete_role(RoleName=LAMBDA_ROLE_NAME)
        logger.info("  ✓ Deleted role: %s", LAMBDA_ROLE_NAME)
    except ClientError as e:
        logger.warning("  ✗ Role: %s", e)

    # 4. Delete IAM policies
    account_id = _get_account_id()
    for policy_name in [f"{PROJECT_NAME}-RdsAccess", f"{PROJECT_NAME}-CostAccess",
                        f"{PROJECT_NAME}-ObservabilityAccess"]:
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        try:
            paginator = iam_client.get_paginator("list_entities_for_policy")
            for page in paginator.paginate(PolicyArn=policy_arn):
                for role in page.get("PolicyRoles", []):
                    iam_client.detach_role_policy(
                        RoleName=role["RoleName"], PolicyArn=policy_arn
                    )
            iam_client.delete_policy(PolicyArn=policy_arn)
            logger.info("  ✓ Deleted policy: %s", policy_name)
        except ClientError as e:
            logger.warning("  ✗ Policy %s: %s", policy_name, e)

    config_path.unlink()
    logger.info("Cleanup complete!")


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--cleanup" in sys.argv:
        cleanup_mcp_lambdas()
    else:
        deploy_mcp_lambdas()
