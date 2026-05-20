# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Setup AgentCore Gateway with Lambda-backed MCP server targets.

This script provisions an AgentCore Gateway for the RDS Well-Architected
Review (WAR) multi-agent system. It creates:

  1. An IAM execution role for the Gateway (outbound auth to Lambda targets)
  2. The Gateway resource with IAM SigV4 inbound auth and semantic search
  3. Registers pre-deployed Lambda MCP servers as Gateway targets

Auth model:
  - Inbound (agent → Gateway): IAM SigV4 — agent signs requests with its
    AWS credentials (profile locally, execution role in AgentCore Runtime).
    No OAuth, no secrets to manage.
  - Outbound (Gateway → Lambda targets): Gateway assumes its IAM role to
    invoke Lambda functions.

Prerequisite:
  Run `python deploy_mcp_lambdas.py` first to deploy the MCP server Lambda
  functions. This script reads mcp_lambdas_config.json to get the Lambda ARNs.

Rollback behavior:
  If any step fails, the script automatically rolls back all previously
  created resources (similar to CloudFormation ROLLBACK_COMPLETE).

Prerequisites:
  - AWS credentials configured (`aws configure` or environment variables)
  - Python packages: `pip install bedrock-agentcore-starter-toolkit boto3`
  - MCP Lambdas deployed: `python deploy_mcp_lambdas.py`
  - IAM permissions: bedrock-agentcore:*, iam:CreateRole, iam:AttachRolePolicy,
    iam:PassRole

Usage:
  python deploy_mcp_lambdas.py        # Step 1: Deploy MCP server Lambdas
  python setup_gateway.py             # Step 2: Create Gateway
  python setup_gateway.py --cleanup   # Remove Gateway resources
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient

# Load environment variables from .env file
load_dotenv(Path(__file__).parent / ".env")

# ── Logging ──────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

REGION: str = os.getenv("AWS_REGION", "us-east-1")
GATEWAY_NAME: str = "rdsAgenticWAR"
GATEWAY_ROLE_NAME: str = "AgentCoreGatewayRole-rdsAgenticWAR"
CONFIG_OUTPUT: str = "gateway_config.json"
IAM_PROPAGATION_DELAY_SECONDS: int = 10


# ── Rollback Stack ───────────────────────────────────────────────────────────


@dataclass
class RollbackStack:
    """Tracks created resources and rolls them back in reverse order on failure.

    Similar to CloudFormation's rollback behavior: if any resource creation
    fails, all previously created resources are deleted in reverse order.
    """

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
                # Log but continue — cleanup proceeds on failure, similar to CloudFormation
                logger.warning("  ✗ Failed to delete %s: %s", resource_name, e)

        self._actions.clear()


# ── Helper functions ─────────────────────────────────────────────────────────


def _get_account_id() -> str:
    """Retrieve the current AWS account ID."""
    sts = boto3.client("sts", region_name=REGION)
    return sts.get_caller_identity()["Account"]


def _clean_tool_schema(tools: list[dict]) -> list[dict]:
    """Clean tool schemas to only include fields accepted by AgentCore Gateway.

    Gateway only accepts: name, description, inputSchema, outputSchema
    inputSchema/outputSchema only accept: type, properties, required, items, description
    """
    ALLOWED_TOOL_FIELDS = {"name", "description", "inputSchema", "outputSchema"}
    ALLOWED_SCHEMA_FIELDS = {"type", "properties", "required", "items", "description"}

    def _clean_schema_obj(schema: dict) -> dict:
        """Recursively clean a JSON Schema object to only allowed fields."""
        cleaned = {}
        for key, value in schema.items():
            if key not in ALLOWED_SCHEMA_FIELDS:
                continue
            if key == "properties" and isinstance(value, dict):
                cleaned_props = {}
                for prop_name, prop_val in value.items():
                    if isinstance(prop_val, dict):
                        clean_prop = _clean_schema_obj(prop_val)
                        # Ensure 'type' exists — use 'string' as fallback
                        if "type" not in clean_prop:
                            clean_prop["type"] = "string"
                        cleaned_props[prop_name] = clean_prop
                    else:
                        cleaned_props[prop_name] = prop_val
                cleaned["properties"] = cleaned_props
            elif key == "items" and isinstance(value, dict):
                clean_items = _clean_schema_obj(value)
                # Ensure items has a type
                if "type" not in clean_items:
                    clean_items["type"] = "object"
                cleaned["items"] = clean_items
            else:
                cleaned[key] = value
        return cleaned

    cleaned_tools = []
    for tool in tools:
        cleaned_tool = {}
        for key in ALLOWED_TOOL_FIELDS:
            if key in tool and tool[key] is not None:
                if key in ("inputSchema", "outputSchema") and isinstance(tool[key], dict):
                    cleaned_tool[key] = _clean_schema_obj(tool[key])
                else:
                    cleaned_tool[key] = tool[key]
        # Ensure required fields exist
        if "name" in cleaned_tool:
            cleaned_tools.append(cleaned_tool)

    return cleaned_tools


def _create_gateway_role(iam_client: Any, account_id: str, stack: RollbackStack) -> str:
    """Create the Gateway execution role with Lambda invoke permissions.

    Returns:
        The role ARN.
    """
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    })

    created_new = False
    try:
        iam_client.create_role(
            RoleName=GATEWAY_ROLE_NAME,
            AssumeRolePolicyDocument=trust_policy,
            Description=f"Gateway execution role for {GATEWAY_NAME}",
            Tags=[
                {"Key": "Project", "Value": GATEWAY_NAME},
                {"Key": "ManagedBy", "Value": "setup_gateway.py"},
            ],
        )
        created_new = True
        iam_client.attach_role_policy(
            RoleName=GATEWAY_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaRole",
        )
        logger.info("  Created IAM role: %s", GATEWAY_ROLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.info("  IAM role already exists: %s", GATEWAY_ROLE_NAME)
        else:
            raise

    role_arn = f"arn:aws:iam::{account_id}:role/{GATEWAY_ROLE_NAME}"

    # Register rollback only if we created the role in this run
    if created_new:
        def _rollback_role():
            try:
                attached = iam_client.list_attached_role_policies(RoleName=GATEWAY_ROLE_NAME)
                for policy in attached.get("AttachedPolicies", []):
                    iam_client.detach_role_policy(
                        RoleName=GATEWAY_ROLE_NAME, PolicyArn=policy["PolicyArn"]
                    )
            except ClientError:
                pass
            iam_client.delete_role(RoleName=GATEWAY_ROLE_NAME)

        stack.push(f"IAM Role: {GATEWAY_ROLE_NAME}", _rollback_role)

    logger.info("  Role ARN: %s", role_arn)
    logger.info("  Waiting %ds for IAM propagation...", IAM_PROPAGATION_DELAY_SECONDS)
    # IAM role creation is eventually consistent — subsequent API calls that
    # reference this role (e.g., Gateway creation) can fail with InvalidInput
    # if the role hasn't propagated yet. This delay is intentional.
    time.sleep(IAM_PROPAGATION_DELAY_SECONDS)  # nosemgrep: arbitrary-sleep
    return role_arn


# ── Main setup ───────────────────────────────────────────────────────────────


def setup_gateway() -> dict[str, Any]:
    """Provision the AgentCore Gateway and all supporting resources.

    On failure, automatically rolls back all created resources.

    Returns:
        Configuration dict (also saved to gateway_config.json).
    """
    logger.info("=" * 70)
    logger.info("RDS Agentic WAR — AgentCore Gateway Setup")
    logger.info("  Region:       %s", REGION)
    logger.info("  Gateway Name: %s", GATEWAY_NAME)
    logger.info("=" * 70)

    iam_client = boto3.client("iam")
    account_id = _get_account_id()
    gateway_client = GatewayClient(region_name=REGION)

    stack = RollbackStack()

    try:
        # Step 1: Create Gateway execution role (outbound auth to Lambda targets)
        logger.info("[Step 1/5] Creating IAM Gateway role...")
        role_arn = _create_gateway_role(iam_client, account_id, stack)

        # Step 2: Create OAuth authorizer (required by Gateway for inbound auth)
        logger.info("[Step 2/5] Creating OAuth authorization server...")
        cognito_response = gateway_client.create_oauth_authorizer_with_cognito(GATEWAY_NAME)
        client_info = cognito_response["client_info"]
        logger.info("  ✓ Authorization server created")

        # Step 3: Create Gateway with OAuth authorizer
        logger.info("[Step 3/5] Creating Gateway...")
        gateway = gateway_client.create_mcp_gateway(
            name=GATEWAY_NAME,
            role_arn=role_arn,
            authorizer_config=cognito_response["authorizer_config"],
            enable_semantic_search=True,
        )
        gateway_url = gateway["gatewayUrl"]
        gateway_id = gateway["gatewayId"]
        logger.info("  Gateway URL: %s", gateway_url)
        logger.info("  Gateway ID:  %s", gateway_id)

        def _rollback_gateway():
            try:
                gateway_client.cleanup_gateway(gateway_id, client_info)
            except Exception as e:
                logger.warning("  Gateway cleanup partial: %s", e)

        stack.push(f"Gateway: {gateway_id}", _rollback_gateway)

        # Step 4: Register pre-deployed Lambda functions as Gateway targets
        # Requires: python deploy_mcp_lambdas.py to be run first
        logger.info("[Step 4/5] Registering Lambda MCP server targets...")

        lambdas_config_path = Path(__file__).parent / "mcp_lambdas_config.json"
        if not lambdas_config_path.exists():
            raise FileNotFoundError(
                "mcp_lambdas_config.json not found. "
                "Run 'python deploy_mcp_lambdas.py' first to deploy the MCP server Lambdas."
            )

        try:
            lambdas_config = json.loads(lambdas_config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"Invalid JSON in mcp_lambdas_config.json: {e}"
            ) from e

        if not isinstance(lambdas_config, dict) or "lambdas" not in lambdas_config:
            raise RuntimeError(
                "mcp_lambdas_config.json is missing required 'lambdas' key."
            )

        deployed_lambdas = [
            entry for entry in lambdas_config.get("lambdas", [])
            if entry.get("status") == "deployed"
        ]

        if not deployed_lambdas:
            raise RuntimeError("No deployed Lambdas found in mcp_lambdas_config.json")

        logger.info("  Found %d deployed Lambda(s)", len(deployed_lambdas))
        targets_created: list[dict[str, str]] = []

        for lambda_info in deployed_lambdas:
            func_name = lambda_info["function_name"]
            func_arn = lambda_info["function_arn"]
            target_name = func_name.replace(f"{GATEWAY_NAME}-mcp-", "")

            logger.info("  Adding target: %s (%s)...", target_name, func_arn)
            try:
                # toolSchema is required for Lambda targets — Gateway can't
                # auto-discover tools from Lambda like it can from HTTP MCP servers
                tool_schema = lambda_info.get("tool_schema", [])
                if not tool_schema:
                    logger.warning("  ⚠️  %s has no tool_schema — skipping", target_name)
                    targets_created.append({
                        "name": target_name,
                        "function_arn": func_arn,
                        "status": "skipped: no tool_schema",
                    })
                    continue

                # Clean the tool schema to only include Gateway-accepted fields
                cleaned_schema = _clean_tool_schema(tool_schema)
                if not cleaned_schema:
                    logger.warning("  ⚠️  %s has no valid tools after cleaning — skipping", target_name)
                    targets_created.append({
                        "name": target_name,
                        "function_arn": func_arn,
                        "status": "skipped: empty schema after cleaning",
                    })
                    continue

                gateway_client.client.create_gateway_target(
                    gatewayIdentifier=gateway_id,
                    name=target_name,
                    targetConfiguration={
                        "mcp": {
                            "lambda": {
                                "lambdaArn": func_arn,
                                "toolSchema": {
                                    "inlinePayload": cleaned_schema,
                                },
                            }
                        }
                    },
                    credentialProviderConfigurations=[
                        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                    ],
                )
                targets_created.append({
                    "name": target_name,
                    "function_arn": func_arn,
                    "status": "created",
                })
                logger.info("  ✓ %s registered", target_name)
            except Exception as e:
                targets_created.append({
                    "name": target_name,
                    "function_arn": func_arn,
                    "status": f"failed: {e}",
                })
                logger.warning("  ✗ %s failed: %s", target_name, e)
                raise RuntimeError(
                    f"Target registration failed for '{target_name}': {e}"
                ) from e

        # Step 5: Verify policies and save configuration
        logger.info("[Step 5/5] Verifying policies and saving configuration...")
        lambda_role_name = lambdas_config.get("execution_role_arn", "").rsplit("/", 1)[-1]
        if lambda_role_name:
            try:
                attached = iam_client.list_attached_role_policies(RoleName=lambda_role_name)
                policy_count = len(attached.get("AttachedPolicies", []))
                logger.info("  ✓ Lambda role '%s' has %d policies attached",
                            lambda_role_name, policy_count)
            except ClientError as e:
                logger.warning("  Could not verify Lambda role policies: %s", e)
        else:
            logger.info("  Skipped — policies managed by deploy_mcp_lambdas.py")

        logger.info("  Saving configuration to %s...", CONFIG_OUTPUT)

        # Store sensitive credentials in AWS Secrets Manager instead of local file
        secrets_client = boto3.client("secretsmanager", region_name=REGION)
        secret_name = f"{GATEWAY_NAME}/gateway-credentials"
        secret_value = json.dumps(client_info)

        try:
            secrets_client.create_secret(
                Name=secret_name,
                Description=f"OAuth credentials for {GATEWAY_NAME} AgentCore Gateway",
                SecretString=secret_value,
                Tags=[
                    {"Key": "Project", "Value": GATEWAY_NAME},
                    {"Key": "ManagedBy", "Value": "setup_gateway.py"},
                ],
            )
            logger.info("  ✓ Credentials stored in Secrets Manager: %s", secret_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceExistsException":
                secrets_client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=secret_value,
                )
                logger.info("  ✓ Credentials updated in Secrets Manager: %s", secret_name)
            else:
                raise

        # Save non-sensitive config to local file (no secrets)
        config: dict[str, Any] = {
            "gateway_url": gateway_url,
            "gateway_id": gateway_id,
            "gateway_role_arn": role_arn,
            "region": REGION,
            "secret_name": secret_name,
            "targets": targets_created,
        }

        config_path = Path(__file__).parent / CONFIG_OUTPUT
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    except Exception as e:
        logger.error("=" * 70)
        logger.error("SETUP FAILED: %s", e)
        logger.error("Initiating rollback (like CloudFormation ROLLBACK_IN_PROGRESS)...")
        logger.error("=" * 70)
        stack.rollback()
        logger.error("=" * 70)
        logger.error("ROLLBACK COMPLETE. No resources left behind.")
        logger.error("Fix the issue above and re-run: python setup_gateway.py")
        logger.error("=" * 70)
        sys.exit(1)

    # Summary (only reached on success)
    successful = [t for t in targets_created if t["status"] == "created"]

    logger.info("=" * 70)
    logger.info("CREATE_COMPLETE — Gateway setup successful!")
    logger.info("  Gateway URL:    %s", gateway_url)
    logger.info("  Gateway ID:     %s", gateway_id)
    logger.info("  IAM Role:       %s", GATEWAY_ROLE_NAME)
    logger.info("  Targets:        %d/%d successful", len(successful), len(deployed_lambdas))
    logger.info("  Config saved:   %s", config_path)
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Add GATEWAY_URL to your .env")
    logger.info("  2. Run: python main.py --local 'Analyze my-cluster'")
    logger.info("=" * 70)

    return config


# ── Cleanup (manual teardown) ────────────────────────────────────────────────


def cleanup_gateway() -> None:
    """Remove all resources created by setup_gateway.

    Reads gateway_config.json to identify resources to delete.
    Deletes in reverse dependency order.
    """
    config_path = Path(__file__).parent / CONFIG_OUTPUT
    if not config_path.exists():
        logger.error("Config file not found: %s — nothing to clean up.", config_path)
        sys.exit(1)

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Invalid JSON in %s: %s", config_path, e)
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("DELETE_IN_PROGRESS — Cleaning up Gateway: %s", config.get("gateway_id", "unknown"))
    logger.info("=" * 70)

    iam_client = boto3.client("iam")
    gateway_client = GatewayClient(region_name=config.get("region", REGION))

    # 1. Delete Gateway and Cognito
    try:
        gateway_client.cleanup_gateway(
            config["gateway_id"],
            config.get("client_info"),
        )
        logger.info("  ✓ Gateway and Cognito resources deleted")
    except ClientError as e:
        logger.warning("  ✗ Gateway cleanup failed: %s", e)
    except Exception as e:
        logger.warning("  ✗ Gateway cleanup failed (unexpected): %s", e)

    # 2. Delete credentials from Secrets Manager
    secret_name = config.get("secret_name", f"{GATEWAY_NAME}/gateway-credentials")
    try:
        secrets_client = boto3.client("secretsmanager", region_name=config.get("region", REGION))
        secrets_client.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
        logger.info("  ✓ Secret deleted: %s", secret_name)
    except ClientError as e:
        logger.warning("  ✗ Could not delete secret: %s", e)

    # 3. Delete Gateway role (detach policies first)
    try:
        attached = iam_client.list_attached_role_policies(RoleName=GATEWAY_ROLE_NAME)
        for policy in attached.get("AttachedPolicies", []):
            iam_client.detach_role_policy(
                RoleName=GATEWAY_ROLE_NAME, PolicyArn=policy["PolicyArn"]
            )
        iam_client.delete_role(RoleName=GATEWAY_ROLE_NAME)
        logger.info("  ✓ Gateway role deleted: %s", GATEWAY_ROLE_NAME)
    except ClientError as e:
        logger.warning("  ✗ Could not delete Gateway role: %s", e)

    # 4. Remove config file
    config_path.unlink()
    logger.info("  ✓ Config file removed")

    logger.info("=" * 70)
    logger.info("DELETE_COMPLETE — All resources cleaned up.")
    logger.info("=" * 70)


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--cleanup" in sys.argv:
        cleanup_gateway()
    else:
        setup_gateway()
