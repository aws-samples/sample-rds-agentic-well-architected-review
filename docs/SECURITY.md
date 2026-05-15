<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Security Design Document

## Overview

This document describes the security architecture, threat model, and security controls for the RDS/Aurora Agentic Well-Architected Review system. It covers authentication, authorization, data protection, and operational security across all components.

## Architecture Security Summary

| Component | Authentication | Authorization | Encryption in Transit | Encryption at Rest |
|---|---|---|---|---|
| AgentCore Runtime | IAM SigV4 | IAM policies | TLS 1.2+ (HTTPS) | Managed by AgentCore |
| AgentCore Gateway | OAuth 2.0 (Cognito) | Cognito scopes | TLS 1.2+ (HTTPS) | Managed by AgentCore |
| Lambda MCP Servers | IAM Role assumption | IAM execution role | TLS 1.2+ (HTTPS) | Managed by Lambda |
| Amazon ECR | IAM credentials | ECR repository policy | TLS 1.2+ (HTTPS) | AES-256 (default) |
| Amazon Bedrock | IAM SigV4 | IAM policies | TLS 1.2+ (HTTPS) | Managed by Bedrock |
| RDS/Aurora (target) | IAM DB auth / rds-db:connect | Read-only IAM policy | TLS (enforced by cluster) | KMS (cluster setting) |
| CloudWatch | IAM SigV4 | Read-only IAM policy | TLS 1.2+ (HTTPS) | Managed by CloudWatch |
| Cost Explorer | IAM SigV4 | Read-only IAM policy | TLS 1.2+ (HTTPS) | Managed by AWS |

## Threat Model

### Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│ Trust Boundary 1: User ↔ AgentCore Runtime                       │
│   Auth: IAM SigV4 or Cognito JWT                                 │
│   Threats: Unauthorized access, prompt injection                 │
├─────────────────────────────────────────────────────────────────┤
│ Trust Boundary 2: Agent ↔ AgentCore Gateway                      │
│   Auth: OAuth 2.0 Bearer token (Cognito)                         │
│   Threats: Token theft, replay attacks                           │
├─────────────────────────────────────────────────────────────────┤
│ Trust Boundary 3: Gateway ↔ Lambda MCP Servers                   │
│   Auth: IAM Role assumption (GATEWAY_IAM_ROLE)                   │
│   Threats: Privilege escalation, lateral movement                │
├─────────────────────────────────────────────────────────────────┤
│ Trust Boundary 4: Lambda ↔ AWS Services (RDS, CW, CE)            │
│   Auth: Lambda execution role (temporary credentials)            │
│   Threats: Over-permissioned access, data exfiltration           │
└─────────────────────────────────────────────────────────────────┘
```

### Threat Analysis

| Threat | Severity | Mitigation | Status |
|---|---|---|---|
| Unauthorized agent invocation | High | IAM SigV4 + Cognito OAuth required for all requests | Implemented |
| Prompt injection attacks | Medium | Input validation (length limit, empty check); agents use read-only tools | Implemented |
| OAuth token theft | Medium | Tokens are short-lived (1 hour); stored only in memory at runtime | Implemented |
| Over-permissioned Lambda role | Medium | Read-only policies; no write/delete/modify actions; documented compensating controls | Mitigated |
| Credential exposure in code | High | No hardcoded secrets; all credentials via IAM roles or env vars | Implemented |
| Data exfiltration via MCP tools | Low | All MCP tools are read-only; no data modification capabilities | Implemented |
| Container image tampering | Low | ECR scan-on-push enabled; images built from official AWS base images | Implemented |
| Insecure temp file usage | Low | `tempfile.mkdtemp()` creates 0o700 directories; cleaned up in `finally` blocks | Implemented |
| Man-in-the-middle attacks | High | HTTPS enforced for all endpoints; explicit scheme validation in code | Implemented |

## Authentication and Authorization

### Inbound Authentication (User → Agent)

- **Local mode**: Direct invocation via CLI (no auth — development only)
- **AgentCore Runtime**: IAM SigV4 signing required; AgentCore Identity validates JWT tokens issued by Amazon Cognito
- **Token lifecycle**: Cognito issues short-lived access tokens (1 hour expiry)

### Outbound Authentication (Agent → MCP Tools)

- **Gateway authentication**: OAuth 2.0 client credentials flow (Cognito)
- **Lambda invocation**: Gateway assumes its IAM role (`AgentCoreGatewayRole-rdsAgenticWAR`) to invoke Lambda functions
- **AWS service access**: Lambda execution role provides temporary credentials via STS

### IAM Policy Design

All IAM policies follow least-privilege with read-only access:

| Policy | Scope | Justification for `Resource: "*"` |
|---|---|---|
| `rds-access.json` | `rds:Describe*`, `rds:List*`, `rds-db:connect` | RDS Describe/List APIs do not support resource-level ARNs per [AWS documentation](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonrds.html) |
| `cost-access.json` | `ce:Get*`, `ce:List*`, `budgets:View*` | Cost Explorer APIs are account-level and do not support resource ARNs |
| `observability-access.json` | `cloudwatch:Get*`, `logs:Get*`, `ec2:DescribeSecurityGroup*` | CloudWatch read APIs require `*` for cross-resource metric queries |

**Compensating controls for wildcard permissions:**
- All actions are strictly read-only (no Create, Update, Delete, Modify, Put)
- Lambda functions run in isolated execution environments
- No network egress beyond AWS service endpoints
- CloudWatch logging captures all API calls for audit

### Shared Execution Role Justification

All five Lambda MCP servers share a single execution role (`McpLambdaExecRole-rdsAgenticWAR`). This is intentional because:
1. All servers require the same read-only access to perform Well-Architected analysis
2. Separate roles would create operational overhead without security benefit (same permissions)
3. The role has no write permissions — compromise of one function cannot escalate privileges

## Data Protection

### Data Classification

| Data Type | Classification | Protection |
|---|---|---|
| AWS credentials | Confidential | IAM roles (never stored); env vars for local dev |
| OAuth tokens | Confidential | In-memory only; 1-hour expiry; HTTPS transport |
| Cognito client secret | Confidential | Stored in `gateway_config.json` (gitignored) |
| Database query results | Internal | In-memory during analysis; included in reports |
| CloudWatch metrics | Internal | Read via HTTPS; included in reports |
| Cost data | Internal | Read via HTTPS; included in reports |
| Generated reports (.docx) | Internal | Written to local filesystem (`~/reports/`) |
| IAM policy documents | Public | Checked into source control (no secrets) |

### Encryption

- **In transit**: All AWS API calls use TLS 1.2+. Gateway URL and token endpoint validated as HTTPS in code.
- **At rest**: Lambda environment variables encrypted by AWS. ECR images encrypted with AES-256. Generated reports stored on local filesystem (user responsibility to encrypt at rest).
- **Key management**: AWS-managed keys for all service-side encryption. No customer-managed KMS keys required for this sample.

### Data Handling Recommendations for Production

- Store generated reports in S3 with SSE-KMS encryption
- Enable CloudTrail for audit logging of all API calls
- Use VPC endpoints for private connectivity to AWS services
- Implement S3 bucket policies to restrict report access

## Network Security

- Lambda functions run in AWS-managed VPC (no public internet access by default)
- All communication uses HTTPS (TLS 1.2+)
- No inbound ports exposed; Lambda functions are invoked via AWS service integration
- Security group access for RDS is read-only inspection (DescribeSecurityGroups)

## Operational Security

### Logging and Monitoring

- Lambda execution logs → CloudWatch Logs (`/aws/lambda/*`)
- Agent token usage and cost tracking → logged per invocation
- Deployment operations → logged via Python `logging` module

### Incident Response

- **Credential compromise**: Rotate Cognito client credentials via `setup_gateway.py --cleanup` followed by re-setup
- **Unauthorized access**: Review CloudTrail logs; revoke IAM role sessions
- **Lambda compromise**: Delete and redeploy via `deploy_mcp_lambdas.py --cleanup` + redeploy

## Security Guidelines by Service

### Amazon RDS / Aurora
- IAM database authentication (`rds-db:connect`) used instead of password-based auth
- Read-only access — no schema modifications, no data writes
- Connection via RDS Data API or IAM-authenticated pgwire (no stored passwords)

### Amazon CloudWatch
- Read-only metric and log access
- `logs:Unmask` permission included to read full log content for analysis (required for operational review)
- No alarm creation or modification permissions

### AWS Cost Explorer
- Read-only cost and usage data access
- Account-scoped (cannot access other accounts' data without cross-account role)
- No budget modification permissions

### Amazon ECR
- Scan-on-push enabled for vulnerability detection
- Images built from `public.ecr.aws/lambda/python:3.12` (official AWS base image)
- No public repository access

### Amazon Cognito
- Used for OAuth 2.0 client credentials flow (machine-to-machine)
- Client secret stored in local config file (gitignored)
- Token endpoint validated as HTTPS before use

## Security Checklist

- [x] No hardcoded credentials in source code
- [x] All endpoints validated as HTTPS
- [x] Input validation for user prompts (length, empty check)
- [x] JSON parsing wrapped in error handling
- [x] Temporary files created securely and cleaned up
- [x] IAM policies are read-only with documented justification
- [x] OAuth tokens are short-lived and in-memory only
- [x] ECR scan-on-push enabled
- [x] Docker images use official AWS base images
- [x] Secrets excluded from version control (.gitignore)
- [x] Deployment scripts support cleanup/rollback
- [ ] CloudTrail enabled (user responsibility)
- [ ] VPC endpoints configured (optional for production)
- [ ] Reports encrypted at rest (user responsibility)
