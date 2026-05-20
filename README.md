<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Amazon RDS/Aurora Agentic Well-Architected Review

## Overview

The RDS Agentic WAR is a multi-agent system that performs automated Well-Architected Reviews for Amazon Relational Database Service (Amazon RDS) and Amazon Aurora databases. Built on the Model Context Protocol (MCP) and powered by Anthropic Claude models via Amazon Bedrock, this system uses specialized AI agents that collaborate to analyze five Well-Architected pillars (Performance, Security, Reliability, Cost Optimization, Operational Excellence) and generate a consolidated executive-ready Word document report. The Amazon Bedrock AgentCore Gateway provides secure access to AWS data sources available as MCP tools.

### Use case details

| Information | Details |
|---|---|
| Use case type | Conversational |
| Agent type | Multi-agent (Coordinator + 5 pillar agents + Report Generator) |
| Use case components | Tools (MCP-based), Amazon CloudWatch, AWS Cost Explorer, Amazon RDS/Aurora, AWS Documentation |
| Use case vertical | Database Operations / Well-Architected Reviews |
| Example complexity | Advanced |
| SDK used | Amazon Bedrock AgentCore SDK, Strands Agents, MCP |

### Use case Architecture

![RDS Agentic WAR Architecture](app/agent/images/rds-agent-architecture.png)

### Use case key Features

- **Multi-Agent Orchestration**: A Coordinator agent routes queries to five pillar-specific agents based on keyword matching, then merges results into a final report
- **Well-Architected Framework Coverage**: Performance Efficiency, Security, Reliability, Cost Optimization, and Operational Excellence pillars
- **MCP-based Integration**: AgentCore Gateway provides secure access to five Lambda-hosted MCP servers with semantic search
- **Automated Report Generation**: Produces a Word document (.docx) with executive summary, per-pillar scores, implementation roadmap, and cost savings quantification
- **Container-based MCP Servers**: Each MCP server runs as a Docker container image on AWS Lambda, avoiding the 250MB zip size limit
- **Rollback-on-Failure Deployment**: Deployment scripts automatically clean up resources if any step fails (CloudFormation-style)
- **Flexible Scope**: Analyze a single pillar, multiple pillars, or run a full 5-pillar review

## Prerequisites

| Requirement | Description |
|---|---|
| Python 3.12+ | Python runtime |
| uv | Python package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/)) |
| Node.js 20.x+ | Required for AWS Cloud Development Kit (AWS CDK) infrastructure |
| Docker | For building MCP server container images |
| AWS CLI | Configured with credentials (`aws configure`) |
| AgentCore CLI | `npm install -g @aws/agentcore-cli` |
| Amazon Bedrock access | Claude models enabled in your region |
| Amazon RDS/Aurora IAM auth or Data API | Target databases must have [IAM database authentication](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAMDBAuth.html) or [Amazon RDS Data API](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html) enabled for the agent to run database-level queries |

## Use case setup

```bash
# Clone the repository
git clone <repository-url>
cd rdsAgenticWAR

# Create and activate a virtual environment
cd app/agent
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env  # Or create .env manually
# Edit .env and set:
#   AWS_REGION=us-east-1
#   AWS_PROFILE_LLM=your-aws-profile  (optional, uses default creds if empty)
#   BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-20250514-v1:0

# Step 1: Deploy MCP server Lambda functions (builds Docker images, pushes to Amazon ECR)
python deploy_mcp_lambdas.py

# Step 2: Create the AgentCore Gateway (Cognito + Gateway + target registration)
python setup_gateway.py

# Step 3: Update .env with the Gateway URL from setup output
# GATEWAY_URL=https://your-gateway-id.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
```

> **Local Setup Complete**: Your RDS Agentic WAR agent is now ready to run locally. If you want to deploy to AgentCore Runtime for production use, follow the [Deployment](#deployment) section below.

## Execution instructions

### Single Query Mode

```bash
# Full 5-pillar Well-Architected review
python main.py --local "Full well-architected review of prod-aurora-cluster"

# Single pillar analysis
python main.py --local "Analyze cost optimization for my-rds-instance"

# Multiple pillars
python main.py --local "Check security and reliability of analytics-cluster"

# General question (no report generated)
python main.py --local "What Aurora PostgreSQL versions are approaching end of support?"

# Filter by tags or attributes
python main.py --local "Review performance of all databases tagged Environment=Production"
```

### Via AgentCore CLI (with hot-reload)

```bash
# From project root
agentcore dev

# Then invoke
agentcore invoke --prompt "Analyze performance of my-cluster"
```

## Development to Production Deployment Flow

The RDS Agentic WAR follows a structured deployment process from local development to production on Amazon Bedrock AgentCore Runtime.

```
STEP 1: LOCAL DEVELOPMENT
┌─────────────────────────────────────────────────────────────────────┐
│  Deploy MCP Lambdas: python deploy_mcp_lambdas.py                   │
│  └─> Setup Gateway: python setup_gateway.py                         │
│      └─> Test locally: python main.py --local "Analyze my-cluster"  │
│          └─> Agent connects to AgentCore Gateway via MCP protocol   │
└─────────────────────────────────────────────────────────────────────┘
                                    ↓
STEP 2: LOCAL DEVELOPMENT WITH AGENTCORE CLI
┌─────────────────────────────────────────────────────────────────────┐
│  Run with hot-reload: agentcore dev                                 │
│  └─> Invoke: agentcore invoke --prompt "Full review of my-cluster"  │
│      └─> Validates agent behavior before production deployment      │
└─────────────────────────────────────────────────────────────────────┘
                                    ↓
STEP 3: PRODUCTION DEPLOYMENT
┌─────────────────────────────────────────────────────────────────────┐
│  Deploy: agentcore deploy                                           │
│  └─> Packages agent code as CodeZip                                 │
│      └─> Deploys to AgentCore Runtime (managed, scalable)           │
│          └─> Test: agentcore invoke --prompt "..."                   │
│              └─> Production agent uses same Gateway + MCP servers    │
└─────────────────────────────────────────────────────────────────────┘

Key Points:
• Core agent code (app/agent/) remains unchanged across all stages
• MCP Lambda servers and Gateway are deployed once, shared by local and production
• Same agent works locally and in production via environment configuration
• AgentCore Gateway provides MCP tool access at all stages
```

## Deployment

Deploy to Amazon Bedrock AgentCore Runtime for production use:

```bash
# From project root
agentcore deploy

# Check status
agentcore status

# Invoke deployed agent
agentcore invoke --prompt "Full review of prod-cluster"

# View logs
agentcore logs
```

## Configuration

| Environment Variable | Description | Default |
|---|---|---|
| `AWS_REGION` | AWS region for all resources | `us-east-1` |
| `AWS_PROFILE_LLM` | AWS profile for Amazon Bedrock calls | (default credentials) |
| `BEDROCK_MODEL_ID` | Amazon Bedrock model for agents | `anthropic.claude-sonnet-4-20250514-v1:0` |
| `GATEWAY_URL` | AgentCore Gateway endpoint | (required — set after setup) |
| `REPORTS_DIR` | Output directory for reports | `~/reports` |

## AgentCore Observability

When deployed to Amazon Bedrock AgentCore Runtime, the agent automatically integrates with Amazon CloudWatch for comprehensive monitoring through the observability primitive.

### Viewing Metrics and Traces

Once deployed with observability enabled, you can monitor your agent's performance through:

- **Amazon CloudWatch Metrics**: View request rates, latencies, and error rates
- **AWS X-Ray Traces**: Analyze distributed traces to understand request flow across the Coordinator and pillar agents
- **Amazon CloudWatch Logs**: Access structured logs for debugging and analysis

The observability primitive automatically captures:
- LLM invocation metrics (tokens, latency, model usage)
- Tool execution traces (duration, success/failure per MCP tool)
- End-to-end request tracing across all agent components
- Per-pillar agent execution time and cycle counts

### Built-in Usage Reporting

When running locally, the agent prints a detailed usage summary after each run including:
- Per-agent token breakdown (input/output)
- Tool call counts per agent
- Estimated cost based on model pricing
- Total execution time and cycle counts

For more details, see the [Amazon Bedrock AgentCore observability documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html).

### Cross-Account Resource Review

To analyze Amazon RDS/Aurora resources in a different AWS account (Account B) from where the agent is deployed (Account A), follow these steps in priority order:

#### Step 1 (CRITICAL — Do First): Create the role in Account B

Create the cross-account role in Account B before granting assume permissions in Account A. This prevents dangling references.

```bash
# Run in Account B (switch credentials or use --profile)
aws iam create-role \
    --role-name RdsAgenticWAR-CrossAccountRole \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::<ACCOUNT_A_ID>:role/McpLambdaExecRole-rdsAgenticWAR"
            },
            "Action": "sts:AssumeRole"
        }]
    }' \
    --tags Key=Project,Value=rdsAgenticWAR Key=ManagedBy,Value=manual

# Attach the read-only policies (same as policies/ folder)
aws iam put-role-policy \
    --role-name RdsAgenticWAR-CrossAccountRole \
    --policy-name RdsAccess \
    --policy-document file://policies/rds-access.json

aws iam put-role-policy \
    --role-name RdsAgenticWAR-CrossAccountRole \
    --policy-name CostAccess \
    --policy-document file://policies/cost-access.json

aws iam put-role-policy \
    --role-name RdsAgenticWAR-CrossAccountRole \
    --policy-name ObservabilityAccess \
    --policy-document file://policies/observability-access.json
```

#### Step 2: Grant assume-role permission in Account A

Add an inline policy to the Lambda execution role allowing it to assume the Account B role:

```bash
# Run in Account A
aws iam put-role-policy \
    --role-name McpLambdaExecRole-rdsAgenticWAR \
    --policy-name CrossAccountAssumeRole \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowCrossAccountAssume",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": "arn:aws:iam::<ACCOUNT_B_ID>:role/RdsAgenticWAR-CrossAccountRole"
        }]
    }'
```

#### Step 3: Verify cross-account access

Test that the trust relationship is working:

```bash
# Run in Account A — attempt to assume the Account B role
aws sts assume-role \
    --role-arn arn:aws:iam::<ACCOUNT_B_ID>:role/RdsAgenticWAR-CrossAccountRole \
    --role-session-name test-cross-account

# If successful, you will see temporary credentials in the output.
# If it fails with "AccessDenied", verify:
#   1. Account B role trust policy has the correct Account A role ARN
#   2. Account A inline policy has the correct Account B role ARN
#   3. Wait 10-15 seconds for IAM propagation after creating policies
```

#### Step 4: Invoke the agent

When prompting the agent, specify the target account's cluster identifier or region. The MCP servers use the assumed role credentials to access Account B's resources:

```bash
python main.py --local "Full review of prod-cluster in account <ACCOUNT_B_ID>"
```

## Security

> **Important:** This sample is provided for educational and demonstration purposes only. It is not intended for production use without additional security review, testing, and hardening.

### Security Controls Implemented

| Layer | Control | Implementation |
|---|---|---|
| Inbound authentication | OAuth 2.0 (Amazon Cognito) | `setup_gateway.py` creates Cognito User Pool; tokens validated by AgentCore Gateway |
| Lambda Function URL auth | AWS_IAM + resource-based policy | `deploy_mcp_lambdas.py` restricts invocation to Gateway role only via `lambda:AddPermission` |
| Gateway → Lambda | IAM Role assumption | `credentialProviderType: GATEWAY_IAM_ROLE` on each target |
| Lambda → AWS services | Temporary STS credentials | Lambda execution role with scoped read-only policies |
| System prompt hardening | Untrusted data instructions | Every agent instructed to treat tool results as data, never as instructions |
| Rate limiting | Sliding window + concurrency | 10 requests/minute, 3 concurrent invocations max |
| Token budget | Per-invocation cap | 500K tokens max per invocation prevents cost spikes |
| ECR image security | Immutable tags + scan-on-push | Tags cannot be overwritten; vulnerabilities detected on push |
| Container images | Official AWS base images | `public.ecr.aws/lambda/python:3.12` with timestamp-based unique tags |

### Known Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Unauthenticated access to Runtime endpoint | High | AgentCore Runtime enforces IAM SigV4 / Cognito JWT at the service level — requests without valid credentials are rejected with HTTP 401 before reaching agent code. Gateway `authorizerType` set to `"OAUTH"` in `agentcore.json`. Forged/expired tokens are rejected by Cognito token validation. |
| No VPC isolation for Lambda MCP servers | Medium | Functions access only AWS service endpoints via HTTPS; add VPC endpoints for production |
| No AWS WAF on AgentCore Runtime endpoint | Medium | Code-level rate limiting implemented; add WAF for IP-based blocking in production |
| Shared IAM execution role across 5 Lambdas | Medium | All Lambdas need the same read-only permissions; split roles for defense-in-depth in production |
| LLM prompt injection (direct and indirect) | Medium | Multi-layer defense (input validation, system prompt hardening, shell restriction, output sanitization); not fully preventable without Amazon Bedrock Guardrails |
| No CloudTrail-level audit for MCP tool calls | Low | Gateway logs tool invocations; enable CloudTrail for full API-level audit in production |
| Reports written to local filesystem unencrypted | Low | Ephemeral storage; use Amazon S3 with SSE-KMS for production report storage |
| No automatic OAuth credential rotation | Low | Credentials in Secrets Manager support rotation; implement `rotation_lambda` for production |

### Shared Responsibility

This project follows the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/). AWS is responsible for security **of** the cloud — the underlying infrastructure, Amazon Bedrock service, AgentCore Runtime, Lambda execution environment, and managed encryption. You are responsible for security **in** the cloud, including:

- Configuring IAM roles with least-privilege permissions appropriate to your environment
- Securing OAuth credentials (stored in AWS Secrets Manager by default)
- Reviewing AI-generated recommendations before implementing any infrastructure changes
- Enabling additional controls (VPC endpoints, AWS WAF, CMK encryption, CloudTrail) for production use
- Configuring [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) for additional prompt injection protection
- Managing access to generated reports containing sensitive infrastructure findings

### Production Deployment

For production use, implement the following additional controls:

| Category | Action |
|---|---|
| **Network isolation** | Deploy Lambda MCP servers in a VPC with VPC endpoints for AWS services (RDS, CloudWatch, Cost Explorer, Bedrock) |
| **WAF protection** | Add AWS WAF to the AgentCore Runtime endpoint with rate-limiting rules and IP allowlisting |
| **Per-Lambda IAM roles** | Create separate execution roles per MCP server (AwsDocs Lambda needs no RDS/CW/CE access) |
| **Encryption at rest** | Store reports in Amazon S3 with SSE-KMS; use customer-managed keys for audit compliance |
| **Credential rotation** | Configure automatic rotation for Cognito client credentials via Secrets Manager rotation Lambda |
| **Audit logging** | Enable AWS CloudTrail for all API calls; enable Amazon CloudWatch Logs Insights for agent trace analysis |
| **Bedrock Guardrails** | Configure content filters, denied topics, and PII detection to harden LLM interactions |
| **Budget alarms** | Set AWS Budgets alerts for Amazon Bedrock inference costs to detect anomalous usage |
| **Monitoring** | Create Amazon CloudWatch dashboards for invocation count, token usage, error rates, and latency |

For the complete threat model and security design, see [`docs/SECURITY.md`](docs/SECURITY.md).
For AI-specific security controls and bias considerations, see [`docs/AI_SECURITY.md`](docs/AI_SECURITY.md).

## Clean up instructions

Remove all deployed AWS resources:

```bash
cd app/agent

# Step 1: Remove Gateway (deletes Cognito + Gateway + targets)
python setup_gateway.py --cleanup

# Step 2: Remove MCP Lambdas (deletes AWS Lambda functions + Amazon ECR + IAM roles + policies)
python deploy_mcp_lambdas.py --cleanup
```

To also remove the AgentCore Runtime deployment:

```bash
# From project root
agentcore destroy
```

## Disclaimer

The examples provided in this repository are for educational purposes only. They demonstrate concepts and techniques. All recommendations, cost estimates, and optimization numbers generated by this agent are based on data collected at the time of analysis and reflect current cluster configuration and usage patterns. Before implementing any recommendation, review the proposed changes thoroughly and assess the potential impact on your application's performance and availability.
