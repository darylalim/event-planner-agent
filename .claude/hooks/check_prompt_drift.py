#!/usr/bin/env python3
"""PostToolUse: fail when prompts.py and the bound tool surface disagree.

The three-file loop (tools/ -> agent.py -> prompts.py) drifts silently.
prompts.py names tools as literal strings, so renaming a tool without updating
the prompt ships an agent instructed to call something that does not exist, and
nothing catches it at import time. No test covers it either -- `grep -rn
prompts tests/` returns nothing.

Both directions are checked:

  phantom   a prompt names a tool that is not bound, so the model is told to
            call a tool it does not have
  missing   a bound tool is never named in any prompt, so the model has no
            instruction telling it the tool exists

Backticked tool *parameters* (`min_capacity`) are legitimate prompt vocabulary,
so they are derived from the tool schemas rather than maintained by hand.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Tools the harness supplies rather than tools/. Regenerate with:
#   grep -rhoE 'name="[a-z_]+"' \
#     .venv/lib/*/site-packages/deepagents/middleware/*.py | sort -u
# `write_todos` comes from langchain's TodoListMiddleware, which agent.py adds
# explicitly because create_deep_agent 0.7.1 does not bind it (see CLAUDE.md).
HARNESS_TOOLS = frozenset(
    {
        "cancel_async_task",
        "check_async_task",
        "compact_conversation",
        "delete",
        "edit_file",
        "execute",
        "glob",
        "grep",
        "list_async_tasks",
        "ls",
        "read_file",
        "rubric_grader",
        "start_async_task",
        "task",
        "update_async_task",
        "write_file",
        "write_todos",
    }
)

#: Backticked lowercase snake_case tokens. Subagent names (`venue-researcher`)
#: contain hyphens and file paths (`/events/...`) start with a slash, so
#: neither is picked up.
TOKEN = re.compile(r"`([a-z_][a-z0-9_]*)`")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    edited = str(data.get("tool_input", {}).get("file_path") or "")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ".")

    # Only the three files that can cause drift are worth the import cost.
    if "src/event_planner" not in Path(edited).as_posix():
        return 0

    prompts = root / "src" / "event_planner" / "prompts.py"
    if not prompts.is_file():
        return 0

    sys.path.insert(0, str(root / "src"))
    try:
        from event_planner.agent import ORCHESTRATOR_TOOLS
        from event_planner.subagents import SUBAGENTS
    except Exception as exc:  # noqa: BLE001 - any import failure is worth surfacing
        print(f"Prompt-drift check could not import the agent: {exc}", file=sys.stderr)
        return 2

    tools = list(ORCHESTRATOR_TOOLS)
    for sub in SUBAGENTS:
        tools.extend(sub.get("tools", []))

    bound = {t.name for t in tools}
    params = {p for t in tools for p in t.args}
    named = set(TOKEN.findall(prompts.read_text()))

    phantom = sorted(named - bound - params - HARNESS_TOOLS)
    missing = sorted(bound - named)
    if not phantom and not missing:
        return 0

    lines = ["prompts.py and the bound tool surface disagree:", ""]
    if phantom:
        lines += [
            f"  instructed but not bound: {', '.join(phantom)}",
            "    prompts.py tells the model to call these, but no tool of that",
            "    name is bound. Fix the name in prompts.py, or bind the tool in",
            "    agent.py / subagents.py.",
            "",
        ]
    if missing:
        lines += [
            f"  bound but never named: {', '.join(missing)}",
            "    these tools are bound but no prompt mentions them, so the model",
            "    has no instruction to use them. Name them in prompts.py, or drop",
            "    them from ORCHESTRATOR_TOOLS / the subagent's tool list.",
            "",
        ]
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
