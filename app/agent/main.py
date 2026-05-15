#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
RDS/Aurora Multi-Agent Well-Architected Review (Strands Version)

Orchestrates five pillar-specific agents + a report generator.
Connects to MCP tools via AgentCore Gateway.
"""

import base64
import contextlib
import json
import logging
import os
import sys
import time
import traceback
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands_tools import shell
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Prevent .pyc file creation — Lambda and container environments use read-only
# filesystems where bytecode caching is unnecessary and can cause write errors.
sys.dont_write_bytecode = True

# Load .env
_APP_DIR = Path(__file__).resolve().parent
load_dotenv(_APP_DIR / ".env")

# ── Config ───────────────────────────────────────────────────────────────────

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_PROFILE_LLM = os.getenv("AWS_PROFILE_LLM", "")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")
GATEWAY_URL = os.getenv("GATEWAY_URL", "")
PROMPTS_DIR = _APP_DIR / "prompts"

PILLAR_DEFINITIONS = {
    "performance": {"label": "Performance", "prompt_file": "performance.md"},
    "security": {"label": "Security", "prompt_file": "security.md"},
    "reliability": {"label": "Reliability", "prompt_file": "reliability.md"},
    "cost_optimization": {"label": "Cost Optimization", "prompt_file": "cost_optimization.md"},
    "ops_excellence": {"label": "Ops Excellence", "prompt_file": "ops_excellence.md"},
}

NOT_ANALYZED = "Not analyzed — excluded from scope"

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "anthropic.claude-sonnet": (0.003, 0.015),
    "anthropic.claude-haiku": (0.0008, 0.004),
    "anthropic.claude-opus": (0.015, 0.075),
    "global.anthropic.claude-sonnet": (0.003, 0.015),
    "global.anthropic.claude-haiku": (0.0008, 0.004),
    "global.anthropic.claude-opus": (0.015, 0.075),
}

# ── Runtime ──────────────────────────────────────────────────────────────────

os.environ["AWS_PAGER"] = ""
app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)


class TokenAccumulator:
    def __init__(self):
        self._lock = threading.Lock()
        self._totals = {"input": 0, "output": 0}
        self._per_agent: list[dict] = []

    def add(self, usage: dict, *, agent_label: str = "",
            cycles: int = 0, tools_called: dict | None = None, stop_reason: str = "") -> None:
        inp = usage.get("inputTokens", 0)
        out = usage.get("outputTokens", 0)
        with self._lock:
            self._totals["input"] += inp
            self._totals["output"] += out
            if agent_label:
                self._per_agent.append({
                    "agent": agent_label, "input": inp, "output": out,
                    "cycles": cycles, "tools_called": tools_called or {},
                    "stop_reason": stop_reason,
                })

    @property
    def totals(self) -> dict[str, int]:
        return dict(self._totals)

    @property
    def per_agent(self) -> list[dict]:
        return list(self._per_agent)


@dataclass
class RunContext:
    output_dir: str
    tools: list
    model: BedrockModel
    tokens: TokenAccumulator = field(default_factory=TokenAccumulator)


_ctx: RunContext | None = None
_bedrock_model: BedrockModel | None = None
_mcp_stack: contextlib.ExitStack | None = None
_mcp_client: MCPClient | None = None
_gateway_tools: list = []

# ── Gateway connection ───────────────────────────────────────────────────────


def connect_gateway() -> None:
    global _gateway_tools, _mcp_stack
    if _mcp_stack is not None:
        return
    logger.info("Connecting to AgentCore Gateway...")
    _mcp_stack = contextlib.ExitStack()

    from mcp.client.streamable_http import streamablehttp_client

    # Get OAuth token from gateway_config.json
    config_path = _APP_DIR / "gateway_config.json"
    if not config_path.exists():
        raise FileNotFoundError("gateway_config.json not found. Run setup_gateway.py first.")
    config = json.loads(config_path.read_text())
    ci = config["client_info"]

    # Validate token endpoint is HTTPS before making the request
    token_endpoint = ci["token_endpoint"]
    if not token_endpoint.startswith("https://"):
        raise ValueError(f"Token endpoint must use HTTPS, got: {token_endpoint}")

    creds = base64.b64encode(f"{ci['client_id']}:{ci['client_secret']}".encode()).decode()
    data = f"grant_type=client_credentials&scope={ci['scope']}".encode()
    req = urllib.request.Request(
        token_endpoint, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {creds}"},
    )
    # URL is from local gateway_config.json (generated by setup_gateway.py, not user input).
    # Scheme is validated as HTTPS above — file:// and custom schemes are rejected.
    with urllib.request.urlopen(req) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected
        access_token = json.loads(resp.read())["access_token"]

    if not access_token:
        raise RuntimeError("Failed to obtain access token from Cognito")

    # Keep the MCP client open for the lifetime of the application.
    # The client must remain in context for tools to work during agent execution.
    global _mcp_client
    _mcp_client = MCPClient(
        lambda: streamablehttp_client(
            GATEWAY_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            terminate_on_close=False,
        )
    )
    _mcp_stack.enter_context(_mcp_client)

    # list_tools_sync returns tool objects that remain usable as long as
    # the client context is open
    _gateway_tools = _mcp_client.list_tools_sync()
    tool_names = [getattr(t, "tool_name", getattr(t, "name", "?")) for t in _gateway_tools]
    logger.info("Gateway connected — %d tools: %s", len(_gateway_tools), ", ".join(tool_names))


def disconnect_gateway() -> None:
    global _mcp_stack, _gateway_tools
    if _mcp_stack is not None:
        _mcp_stack.close()
        _mcp_stack = None
        _gateway_tools = []
        logger.info("Gateway disconnected")


# ── Bedrock model ────────────────────────────────────────────────────────────


def _get_bedrock_model() -> BedrockModel:
    global _bedrock_model
    if _bedrock_model is None:
        session_kwargs = {"region_name": AWS_REGION}
        if AWS_PROFILE_LLM:
            session_kwargs["profile_name"] = AWS_PROFILE_LLM
        session = boto3.Session(**session_kwargs)
        _bedrock_model = BedrockModel(
            model_id=BEDROCK_MODEL_ID, max_tokens=25000, temperature=0.1,
            boto_client_config=Config(read_timeout=900, connect_timeout=60,
                                     retries={"max_attempts": 5, "mode": "adaptive"}),
            boto_session=session,
        )
    return _bedrock_model


# ── Agent helpers ────────────────────────────────────────────────────────────


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text()


def _build_system_prompt(role: str, prompt_file: str) -> str:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = f"You are {role}. {_load_prompt(prompt_file)}\n\nTODAY'S DATE: {today}\n"
    if _ctx and _ctx.output_dir:
        base += (f"OUTPUT DIRECTORY: {_ctx.output_dir}\n"
                 "Save all output files to this directory.\n\n"
                 "When using the shell tool, always set non_interactive=True.")
    return base


def _run_agent(label: str, role: str, prompt_file: str, prompt: str,
               *, include_shell: bool = True) -> str:
    if _ctx is None:
        raise RuntimeError("RunContext not initialized — call analyze_query() first.")
    logger.info("  [%s] Starting analysis...", label)
    start = time.time()
    if include_shell and _ctx.output_dir:
        os.makedirs(_ctx.output_dir, exist_ok=True)
    # All tools come from the Gateway — pass the live tool list from the client
    # The tools reference the MCP client session which must remain open
    tools: list = [_gateway_tools]
    if include_shell:
        tools += [shell]
    agent = Agent(
        model=_ctx.model,
        system_prompt=_build_system_prompt(role, prompt_file),
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(window_size=10),
    )
    result = agent(prompt, invocation_state={"max_cycles": 15})
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) or {}
    cycle_count = getattr(metrics, "cycle_count", 0) or 0
    stop_reason = getattr(result, "stop_reason", "") or ""
    tool_summary = {
        name: getattr(tm, "call_count", 0)
        for name, tm in (getattr(metrics, "tool_metrics", {}) or {}).items()
    }
    if usage:
        _ctx.tokens.add(usage, agent_label=label, cycles=cycle_count,
                        tools_called=tool_summary, stop_reason=stop_reason)
    logger.info("  [%s] Complete (%.1fs, %d cycles)", label, time.time() - start, cycle_count)
    return str(result)


# ── Pillar tools ─────────────────────────────────────────────────────────────

MAX_PILLAR_RETRIES = 2


def _run_pillar_with_retry(pillar_key: str, query: str) -> str:
    defn = PILLAR_DEFINITIONS[pillar_key]
    last_error = None
    for attempt in range(1, MAX_PILLAR_RETRIES + 2):
        try:
            return _run_agent(defn["label"], "an AWS expert", defn["prompt_file"], query)
        except Exception as e:
            last_error = e
            if attempt <= MAX_PILLAR_RETRIES:
                logger.warning("  [%s] Attempt %d failed: %s. Retrying...",
                               defn["label"], attempt, e)
    return f"{defn['label']} analysis failed: {last_error}"


@tool
def analyze_performance(query: str) -> str:
    """Analyze the Performance Efficiency pillar for an RDS/Aurora database."""
    return _run_pillar_with_retry("performance", query)

@tool
def analyze_security(query: str) -> str:
    """Analyze the Security pillar for an RDS/Aurora database."""
    return _run_pillar_with_retry("security", query)

@tool
def analyze_reliability(query: str) -> str:
    """Analyze the Reliability pillar for an RDS/Aurora database."""
    return _run_pillar_with_retry("reliability", query)

@tool
def analyze_cost_optimization(query: str) -> str:
    """Analyze the Cost Optimization pillar for an RDS/Aurora database."""
    return _run_pillar_with_retry("cost_optimization", query)

@tool
def analyze_ops_excellence(query: str) -> str:
    """Analyze the Operational Excellence pillar for an RDS/Aurora database."""
    return _run_pillar_with_retry("ops_excellence", query)

@tool
def analyze_generic(query: str) -> str:
    """Answer a general RDS/Aurora/AWS question."""
    try:
        return _run_agent("Generic", "a helpful AWS database expert", "generic.md",
                          query, include_shell=False)
    except Exception as e:
        return f"Generic analysis failed: {e}"

@tool
def generate_report(query: str, performance_report: str = NOT_ANALYZED,
                    security_report: str = NOT_ANALYZED,
                    reliability_report: str = NOT_ANALYZED,
                    cost_report: str = NOT_ANALYZED,
                    ops_report: str = NOT_ANALYZED) -> str:
    """Merge pillar reports into a final Well-Architected Word document."""
    try:
        pillars = [("Performance Efficiency", performance_report),
                   ("Security", security_report), ("Reliability", reliability_report),
                   ("Cost Optimization", cost_report),
                   ("Operational Excellence", ops_report)]
        analyzed, excluded, sections = [], [], []
        for name, report in pillars:
            if "Not analyzed" in report or "excluded from scope" in report:
                excluded.append(name)
            else:
                analyzed.append(name)
                sections.append(f"## {name}\n{report}")
        scope = f"Pillars analyzed: {', '.join(analyzed) or 'None'}"
        if excluded:
            scope += f"\nPillars excluded: {', '.join(excluded)}"
        prompt = (f"Generate the final Well-Architected report.\n"
                  f"User request: {query}\n\n### Analysis Scope\n{scope}\n\n"
                  + "\n\n".join(sections))
        return _run_agent("Report Generator", "a technical writer",
                          "report_generator.md", prompt)
    except Exception as e:
        return f"Report generation failed: {e}"


COORDINATOR_TOOLS = [
    analyze_performance, analyze_security, analyze_reliability,
    analyze_cost_optimization, analyze_ops_excellence,
    analyze_generic, generate_report,
]

# ── Usage reporting ──────────────────────────────────────────────────────────


def _print_usage_summary(elapsed: float) -> None:
    if _ctx is None:
        raise RuntimeError("RunContext not initialized.")
    totals = _ctx.tokens.totals
    lines = [f"\n{'─' * 90}", f"⏱  Total time: {elapsed:.1f}s", "",
             "📊 Per-agent breakdown:",
             f"  {'Agent':<22} {'Cycles':>6} {'Input':>10} {'Output':>10} {'Total':>10}  Stop",
             f"  {'─' * 75}"]
    for e in _ctx.tokens.per_agent:
        lines.append(f"  {e['agent']:<22} {e['cycles']:>6} {e['input']:>10,} "
                     f"{e['output']:>10,} {e['input']+e['output']:>10,}  {e['stop_reason']}")
    lines += [f"  {'─' * 75}", "", "🔧 Tool calls:"]
    for e in _ctx.tokens.per_agent:
        if e["tools_called"]:
            lines.append(f"  {e['agent']:<22} "
                         + ", ".join(f"{n}({c})" for n, c in e["tools_called"].items()))
    inp, out = totals["input"], totals["output"]
    lines += ["", f"📊 Total ({BEDROCK_MODEL_ID}):",
              f"  input {inp:,} | output {out:,} | total {inp+out:,}"]
    for prefix, (ip, op) in MODEL_PRICING.items():
        if BEDROCK_MODEL_ID.startswith(prefix):
            lines.append(f"\n💰 Estimated cost: ${(inp/1000*ip)+(out/1000*op):.4f}")
            break
    lines.append("─" * 90)
    logger.info("\n".join(lines))


# ── Orchestration ────────────────────────────────────────────────────────────


def analyze_query(query: str) -> str:
    global _ctx
    try:
        epoch = int(time.time())
        reports_base = os.getenv("REPORTS_DIR", os.path.expanduser("~/reports"))
        _ctx = RunContext(output_dir=f"{reports_base}/{epoch}",
                          tools=_gateway_tools, model=_get_bedrock_model())
        coordinator = Agent(
            model=_ctx.model,
            system_prompt=_build_system_prompt("the coordinator agent", "coordinator.md"),
            tools=COORDINATOR_TOOLS,
        )
        logger.info("Starting multi-agent analysis...")
        start = time.time()
        result = coordinator(f"Analyze: {query}")
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None) or {}
        if usage:
            _ctx.tokens.add(usage, agent_label="Coordinator",
                            cycles=getattr(metrics, "cycle_count", 0) or 0,
                            stop_reason=getattr(result, "stop_reason", "") or "")
        _print_usage_summary(time.time() - start)
        logger.info("Multi-agent analysis complete")
        return str(result)
    except Exception as e:
        logger.error("Analysis failed: %s\n%s", e, traceback.format_exc())
        return f"Analysis failed: {type(e).__name__}: {e}"


# ── Entrypoints ──────────────────────────────────────────────────────────────


@app.entrypoint
async def invoke(payload: dict) -> str:
    """Stream the analysis progress and final result."""
    connect_gateway()
    query = payload.get("prompt", "")

    yield "Starting analysis...\n"
    result = analyze_query(query)
    yield result

    # Include cost summary in the streamed response
    if _ctx:
        totals = _ctx.tokens.totals
        inp, out = totals["input"], totals["output"]
        yield f"\n\n───────────────────────────────────────\n"
        yield f"📊 Tokens: input {inp:,} | output {out:,} | total {inp+out:,}\n"
        for prefix, (ip, op) in MODEL_PRICING.items():
            if BEDROCK_MODEL_ID.startswith(prefix):
                cost = (inp / 1000 * ip) + (out / 1000 * op)
                yield f"💰 Estimated cost: ${cost:.4f}\n"
                break


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--local" in sys.argv:
        prompt = " ".join(a for a in sys.argv[1:] if a != "--local") or input("Enter prompt: ")
        try:
            connect_gateway()
            print("\n\n📋 FINAL RESULT:\n")
            print(analyze_query(prompt))
        finally:
            disconnect_gateway()
    else:
        app.run()
