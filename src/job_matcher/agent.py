"""An agent that answers questions about the offers by calling tools.

Two ways to run it:
  run()             a function-calling loop written here, through the OpenAI SDK; works with any
                    OpenAI-compatible provider (OpenAI, Azure OpenAI, Gemini, Ollama)
  run_claude_cli()  Claude Code in headless mode with the MCP server from mcp_server.py as its only
                    tools; runs on the user's own subscription, so local use only
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from job_matcher.llm import ChatModel
from job_matcher.tools import Toolbox

SYSTEM = """You help a job seeker in Poland find and compare open job offers. Use the tools for every fact
about offers; never name an offer, company or link the tools did not return. Search with the user's words
or their CV, then open the most promising offers with get_offer before you judge them. When the user gives
no level, assume they want entry-level roles (the search defaults). Answer in the user's language, briefly,
and link each offer you mention."""


def _assistant_message(msg) -> dict:
    """Keep provider extras on tool calls (Gemini returns thought signatures it needs back next turn)."""
    out = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in msg.tool_calls]
    return out


def run(question: str, toolbox: Toolbox, model: ChatModel, max_steps: int = 8, log=print) -> str:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    tools = toolbox.openai_tools()
    for _ in range(max_steps):
        msg = model.chat(messages, tools)
        messages.append(_assistant_message(msg))
        if not msg.tool_calls:
            return msg.content or ""
        for tc in msg.tool_calls:
            log(f"  -> {tc.function.name}({tc.function.arguments[:150]})")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": toolbox.call(tc.function.name, tc.function.arguments)})
    messages.append({"role": "user", "content": "Stop calling tools and answer with what you have."})
    return model.chat(messages, tools=[]).content or ""


TOOL_NAMES = ["search_offers", "get_offer", "skill_demand", "catalog_info"]


def run_claude_cli(question: str, model: str = "haiku", log=print) -> tuple[str, float]:
    """Returns the answer and the API-equivalent cost the CLI reports."""
    server = {"command": sys.executable, "args": ["-m", "job_matcher.mcp_server"], "cwd": str(Path.cwd()),
              "env": {k: v for k, v in os.environ.items() if k.startswith(("JM_", "DATABASE_URL"))}}
    cmd = ["claude", "-p", "--model", model, "--system-prompt", SYSTEM,
           "--mcp-config", json.dumps({"mcpServers": {"jobs": server}}), "--strict-mcp-config",
           "--tools", "", "--allowedTools", ",".join(f"mcp__jobs__{t}" for t in TOOL_NAMES),
           "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--setting-sources", ""]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    proc.stdin.write(question)
    proc.stdin.close()
    answer, cost = "", 0.0
    for line in proc.stdout:
        ev = json.loads(line)
        if ev.get("type") == "assistant":
            for part in ev["message"].get("content", []):
                if part.get("type") == "tool_use":
                    log(f"  -> {part['name'].removeprefix('mcp__jobs__')}({json.dumps(part['input'])[:150]})")
        elif ev.get("type") == "result":
            answer, cost = ev.get("result") or "", ev.get("total_cost_usd") or 0.0
    proc.wait()
    return answer, cost
