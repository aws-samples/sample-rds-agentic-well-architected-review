<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# AI Security, Compliance, and Responsible Use

## Overview

This document covers AI/ML security controls, third-party component compliance, dataset handling, model security, and bias/fairness considerations for the Amazon RDS/Aurora Agentic Well-Architected Review system.

## GenAI Use Case Classification

| Attribute | Value |
|---|---|
| Use case type | Operational analysis and advisory (non-binding recommendations) |
| Risk category | Low — no health, financial, legal, or safety-critical decisions |
| Data sensitivity | Internal — reads existing AWS resource configurations and metrics |
| Output type | Advisory report with recommendations (human review required before action) |
| Autonomy level | Read-only — agent cannot modify infrastructure or data |

## Third-Party Components

### LLM Providers

| Component | Provider | Access Method | Approval Status |
|---|---|---|---|
| Claude Sonnet 4 | Anthropic | Amazon Bedrock (AWS Marketplace) | Pre-approved — accessed via Amazon Bedrock managed service |

> **Note**: The `AGENTS.md` file references additional providers (OpenAI, Gemini) as supported by the AgentCore framework. This project uses **only Amazon Bedrock** for inference. No direct API calls are made to third-party LLM providers.

### MCP Server Packages

| Package | Publisher | License | Purpose |
|---|---|---|---|
| `awslabs.aws-api-mcp-server` | AWS Labs | Apache-2.0 | AWS API calls (read-only) |
| `awslabs.postgres-mcp-server` | AWS Labs | Apache-2.0 | PostgreSQL/Aurora queries |
| `awslabs.cloudwatch-mcp-server` | AWS Labs | Apache-2.0 | Amazon CloudWatch metrics and logs |
| `awslabs.billing-cost-management-mcp-server` | AWS Labs | Apache-2.0 | AWS Cost Explorer data |
| `awslabs.aws-documentation-mcp-server` | AWS Labs | Apache-2.0 | AWS documentation search |

All MCP servers are published by AWS Labs under Apache-2.0 license and are designed for use with Amazon Bedrock AgentCore.

### Agent Frameworks

| Package | Publisher | License | Purpose |
|---|---|---|---|
| `strands-agents` | AWS | Apache-2.0 | Multi-agent orchestration |
| `strands-agents-tools` | AWS | Apache-2.0 | Tool integration (shell) |
| `bedrock-agentcore` | AWS | Apache-2.0 | AgentCore Runtime SDK |
| `mcp` | Anthropic | MIT | Model Context Protocol client |
| `run-mcp-servers-with-aws-lambda` | AWS | Apache-2.0 | Lambda MCP wrapper |

## AI/ML Security Controls

### Input Validation

| Control | Implementation | Location |
|---|---|---|
| Prompt length limit | Maximum 10,000 characters | `main.py` — `_validate_prompt()` |
| Empty prompt rejection | Rejects blank/whitespace-only input | `main.py` — `_validate_prompt()` |
| URL scheme validation | HTTPS-only for all external endpoints | `main.py` — `connect_gateway()` |

### Output Controls

| Control | Implementation |
|---|---|
| Read-only tools | All MCP servers have read-only IAM policies — agent cannot modify resources |
| Cycle limits | `max_cycles=15` per agent invocation prevents runaway execution |
| Conversation windowing | `SlidingWindowConversationManager(window_size=10)` limits context size |
| Structured output | Report generator follows a fixed template structure |

### Prompt Injection Mitigation

| Control | Description |
|---|---|
| System prompt isolation | Each agent has a dedicated system prompt loaded from file — user input is passed as the user message, not injected into the system prompt |
| Tool-level constraints | MCP tools enforce their own input validation (e.g., SQL parameterization in Postgres MCP server) |
| Read-only execution | Even if prompt injection succeeds, the agent can only read data — no destructive actions are possible |
| Scope limitation | Coordinator routes to specific pillar agents — a single agent cannot access tools outside its scope |

### Data Sanitization

| Data Flow | Sanitization |
|---|---|
| User prompt → Agent | Validated for length and emptiness |
| Agent → MCP tools | Tool schemas enforce parameter types and required fields |
| MCP tools → AWS APIs | IAM policies restrict to read-only actions |
| Agent → Report output | Written to local filesystem; no data sent to external services |

## Model Security

### Access Controls

| Control | Implementation |
|---|---|
| Model access | Amazon Bedrock IAM policies control which models can be invoked |
| API authentication | All Bedrock calls use IAM SigV4 signing |
| Region restriction | Model calls restricted to configured `AWS_REGION` |
| Retry limits | Adaptive retry with max 5 attempts prevents excessive API usage |

### Model Usage Monitoring

| Metric | Tracked |
|---|---|
| Input/output tokens per agent | Yes — `TokenAccumulator` class |
| Cost estimation per invocation | Yes — logged after each run |
| Cycle count per agent | Yes — prevents infinite loops |
| Tool call frequency | Yes — per-agent tool metrics |

### Model Configuration

| Parameter | Value | Rationale |
|---|---|---|
| `temperature` | 0.1 | Low temperature for consistent, factual analysis |
| `max_tokens` | 25,000 | Sufficient for detailed pillar reports |
| `max_cycles` | 15 | Prevents runaway tool-calling loops |

## Dataset Compliance

### Data Sources

This system does **not** use training data, fine-tuned models, or custom datasets. All data is:

| Source | Type | Compliance |
|---|---|---|
| AWS API responses | Live infrastructure metadata | Customer-owned data, accessed with customer credentials |
| Amazon CloudWatch metrics | Time-series operational data | Customer-owned, read-only access |
| AWS Cost Explorer | Billing and usage data | Customer-owned, account-scoped |
| PostgreSQL system catalogs | Database metadata (pg_stat_*) | Customer-owned, read via IAM auth |
| AWS Documentation | Public documentation | Publicly available, no licensing concerns |

### Data Retention

- **In-memory only**: All data retrieved during analysis exists only in agent memory during execution
- **Report output**: Written to local filesystem (`~/reports/`) — retention is user-controlled
- **No training**: Customer data is never used to train or fine-tune models (Amazon Bedrock guarantee)
- **No persistence**: No data is stored in the agent system between invocations

## Bias and Fairness Considerations

### Scoring Methodology

The Well-Architected pillar scores (0-100) are generated by the LLM based on evidence collected from AWS APIs. Important considerations:

| Consideration | Mitigation |
|---|---|
| Scores are qualitative, not algorithmic | Report explicitly states scores are LLM-generated assessments, not deterministic calculations |
| LLM may have training biases toward certain configurations | Prompts require evidence-based scoring — every score must cite specific metrics or findings |
| Recommendations may favor newer/more expensive services | Cost optimization pillar explicitly validates pricing from official AWS documentation |
| Different model versions may produce different scores | Model ID is pinned in configuration for reproducibility |

### Transparency Requirements

The generated report includes:
- **Disclaimer**: States that recommendations are advisory and require human review
- **Data sources**: Lists which AWS APIs and metrics were consulted
- **Limitations**: Notes what could not be analyzed (e.g., application-level performance)
- **Score rationale**: Each pillar score includes specific evidence citations

### Fairness in Recommendations

| Principle | Implementation |
|---|---|
| No vendor lock-in bias | Agent recommends AWS-native solutions (appropriate for AWS-hosted databases) |
| Cost-neutral analysis | Recommendations include both cost-saving and cost-increasing options with justification |
| Workload-appropriate | Prompts instruct agents to consider the specific workload pattern, not apply generic rules |
| Human-in-the-loop | All recommendations require human review before implementation (stated in disclaimer) |

## Responsible Use Guidelines

### Intended Use

- Automated Well-Architected Reviews for Amazon RDS and Amazon Aurora databases
- Advisory recommendations for database optimization
- Operational health assessments with evidence-based scoring

### Not Intended For

- Automated infrastructure changes without human approval
- Compliance certification or audit attestation
- Financial decisions without independent verification
- Security vulnerability scanning (use dedicated tools like Amazon Inspector)

### User Responsibilities

- Review all recommendations before implementing
- Validate cost estimates against current AWS pricing
- Test changes in non-production environments first
- Maintain appropriate IAM permissions and access controls
- Configure Amazon Bedrock Guardrails for additional content filtering if needed

## Amazon Bedrock Guardrails

For production deployments, consider enabling [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) to provide additional protection against:
- Prompt injection attempts
- Inappropriate content generation
- Sensitive information disclosure
- Off-topic responses

Guardrails can be configured at the Amazon Bedrock model level and apply transparently to all agent invocations.
