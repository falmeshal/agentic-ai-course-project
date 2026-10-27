# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "pandas>=2.2", "mcp>=2.3"]
# ///
"""Istirdad: a refund assistant for Madar Municipal Services. Model calls go to OpenRouter (OPENROUTER_API_KEY in .env).

    uv run istirdad.py ask "My sign permit SGN-40220 was rejected, I want my fee back. ID ends 0093, mobile 0540410093."
    uv run istirdad.py eval tasks.jsonl --config A

Configuration A is the baseline: one agent loop (the course capstone starter) with the refund skill in its prompt.
Configuration B, the graph, comes next.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp import StdioServerParameters  # noqa: E402

import agentkit as K  # noqa: E402
import evals  # noqa: E402
import madar  # noqa: E402

OUT = K.lab_dir("istirdad")

SKILL = (ROOT / "data" / "skills" / "refund-request" / "SKILL.md").read_text(encoding="utf-8")
SPEC = madar.SYSTEM_PROMPT + "\n\n" + SKILL  # ponytail: replaced by AGENTS.md when the graph lands

CONFIGS = {
    "A": {"system": SPEC, "model": None},  # model None = K.MODEL (AGENT_MODEL in .env, default a free OpenRouter model)
}

MAX_STEPS, MAX_COST, MAX_RESULT_CHARS = 8, 0.05, 3000
WRITE_TOOLS = {"create_refund", "send_email"}


def local_tools(m: madar.Madar) -> list:
    """Write tools live in this process; read tools (get_permit, search_policies, …) come over MCP.
    send_email is the course's open tool (any recipient): the baseline the security defence is measured against."""
    return [m.create_refund, m.send_email]


def mcp_server() -> StdioServerParameters:
    return StdioServerParameters(command="uv", args=["run", "--quiet", str(ROOT / "servers" / "madar_mcp.py")])


def permission(name: str, args: dict) -> str:
    return "ask" if name in WRITE_TOOLS else "allow"


def approve_cli(name: str, args: dict) -> bool:
    return input(f"\n🛡️  Allow {name}({json.dumps(args, ensure_ascii=False)})? [y/N] ").strip().lower() == "y"


def cap_result(name: str, args: dict, result: str) -> str | None:
    """after_tool hook: log every call, and keep tool results inside the context budget."""
    with (OUT / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": name, "args": args, "chars": len(result)}, ensure_ascii=False) + "\n")
    if len(result) > MAX_RESULT_CHARS:
        return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
    return None


def make_agent(config: dict, mcp: K.MCPBridge, approve=approve_cli):
    def agent(prompt: str, m: madar.Madar, **kw) -> K.Run:
        return K.run_agent(prompt, local_tools(m) + mcp.agent_tools(), system=config["system"], model=config["model"],
                           max_steps=MAX_STEPS, max_cost=MAX_COST, permission=permission, approve=approve,
                           after_tool=cap_result, verbose=kw.pop("verbose", False), **kw)
    return agent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ask").add_argument("question")
    e = sub.add_parser("eval")
    e.add_argument("tasks", type=Path)
    e.add_argument("--config", nargs="+", default=list(CONFIGS))
    e.add_argument("--trials", type=int, default=1)
    args = ap.parse_args()

    print(f"provider={K.PROVIDER} model={K.MODEL}")
    with K.MCPBridge(mcp_server()) as mcp:
        if args.cmd == "ask":
            run = make_agent(CONFIGS["A"], mcp)(args.question, madar.Madar(OUT / "db"), verbose=True)
            print("\n" + (run.final or f"(stopped: {run.stopped})"))
        else:
            import pandas as pd

            tasks = evals.load_tasks(args.tasks)
            consent = lambda name, a: True  # noqa: E731 - unattended: task prompts give consent; db graders check writes
            for c in args.config:
                df = evals.run_suite(make_agent(CONFIGS[c], mcp, approve=consent), tasks, trials=args.trials, name=c)
                df.drop(columns="final").to_csv(ROOT / f"eval_{c}.csv", index=False)
                print(f"\n{c}:", evals.report(df)[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
