#!/usr/bin/env python3
"""PostToolUse: fail when the prompts and the bound tool surface disagree.

The three-file loop (tools/ -> agent.py -> prompts.py) drifts silently.
prompts.py names tools as literal strings, so renaming a tool without updating
the prompt ships an agent instructed to call something that does not exist, and
nothing catches it at import time. No test covers it either -- `grep -rn
prompts tests/` returns nothing.

Four checks, each a different failure:

  misrouted   a prompt names a real tool that *this* agent was not given.
              Checked per agent, because that is the binding that decides what
              the model can actually call: budget-analyst's prompt naming
              `hold_venue` is a real defect even though hold_venue is bound to
              the orchestrator.

  unknown     a prompt or skill names something that is not a tool anywhere.
              A typo, or prose that reads like a call.

  missing     a bound tool is named by no prompt and no skill. Checked
              GLOBALLY, not per agent: the orchestrator legitimately names only
              2 of its 7 tools and delegates the rest, so a per-agent version
              of this check reports five false positives on the first run.

  ungated     a name in IRREVERSIBLE_TOOLS is not a bound tool. INTERRUPT_ON is
              derived from that list, so a rename leaves the gate keyed to a
              tool that no longer exists and the booking executes unreviewed.
              test_every_irreversible_tool_is_gated cannot catch this: it
              asserts set(INTERRUPT_ON) == set(IRREVERSIBLE_TOOLS), and
              INTERRUPT_ON is built from IRREVERSIBLE_TOOLS, so it is true by
              construction.

Skills are scanned alongside prompts: workspace/skills/*/SKILL.md is loaded
into the model's context at runtime, names tools in backticks the same way, and
is the one subtree under workspace/ the guards allow writing to.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Tools the harness supplies rather than tools/. Snapshot of the installed
# deepagents; regenerate with:
#   grep -rhoE 'name="[a-z_]+"' \
#     .venv/lib/*/site-packages/deepagents/middleware/*.py | sort -u
# `write_todos` comes from langchain's TodoListMiddleware, which agent.py adds
# explicitly because create_deep_agent 0.7.1 does not bind it (see CLAUDE.md).
# Going stale here causes a false "unknown", never a missed defect, and the
# message below says so.
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

#: Prose that is legitimately backticked in a prompt without being a tool.
#: `approve`/`edit`/`reject` are imported rather than written out: they are the
#: operator vocabulary in agent.py, and a prompt explaining the approval gate
#: has every reason to name them.
EXTRA_NON_TOOL_TERMS = frozenset({"json", "markdown", "md", "yaml"})

#: Backticked lowercase snake_case tokens. Subagent names (`venue-researcher`)
#: contain hyphens and file paths (`/events/...`) start with a slash, so
#: neither is picked up.
TOKEN = re.compile(r"`([a-z_][a-z0-9_]*)`")

#: Editing any of these can desync a prompt from the tools. Kept in step with
#: the docstring above rather than admitting the whole package.
TRIGGERS = (
    "src/event_planner/prompts.py",
    "src/event_planner/subagents.py",
    "src/event_planner/agent.py",
    "src/event_planner/tools/",
    "workspace/skills/",
)


def tool_params(tool: Any) -> set[str]:
    """Argument names, which are legitimate backticked prompt vocabulary."""
    try:
        return set(tool.args)
    except Exception:  # noqa: BLE001 - a tool without a schema is not fatal here
        return set()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    edited = str(payload.get("tool_input", {}).get("file_path") or "")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")
    posix = Path(edited).as_posix()
    if not edited or not any(t in posix for t in TRIGGERS):
        return 0

    sys.path.insert(0, str(root / "src"))
    try:
        from event_planner.agent import ALLOWED_DECISIONS, ORCHESTRATOR_TOOLS
        from event_planner.prompts import ORCHESTRATOR_PROMPT
        from event_planner.subagents import SUBAGENTS
        from event_planner.tools import IRREVERSIBLE_TOOLS
    except Exception as exc:  # noqa: BLE001 - a half-finished rename lands here
        print(f"Prompt-drift check could not import the agent: {exc}", file=sys.stderr)
        print("A partially applied rename looks exactly like this.", file=sys.stderr)
        return 2

    # The pairing is read from the same structures the harness uses, so it
    # cannot drift from what is actually bound.
    agents: list[tuple[str, str, list[Any]]] = [
        ("orchestrator", ORCHESTRATOR_PROMPT, list(ORCHESTRATOR_TOOLS))
    ]
    agents += [
        (str(s["name"]), str(s.get("system_prompt", "")), list(s.get("tools", [])))
        for s in SUBAGENTS
    ]

    every_tool = [t for _, _, tools in agents for t in tools]
    bound = {t.name for t in every_tool}
    params = {p for t in every_tool for p in tool_params(t)}
    benign = params | HARNESS_TOOLS | EXTRA_NON_TOOL_TERMS | set(ALLOWED_DECISIONS)

    problems: list[str] = []
    named_anywhere: set[str] = set()

    for name, prompt, tools in agents:
        own = {t.name for t in tools}
        named = set(TOKEN.findall(prompt))
        named_anywhere |= named
        misrouted = sorted((named & bound) - own)
        unknown = sorted(named - bound - benign)
        if misrouted:
            problems.append(
                f"  {name}: names {', '.join(misrouted)}, which is bound to a "
                f"different agent.\n"
                f"    This prompt can only call: {', '.join(sorted(own)) or '(none)'}.\n"
                f"    Either add the tool to this agent, or stop naming it here."
            )
        if unknown:
            problems.append(
                f"  {name}: names {', '.join(unknown)}, which is not a tool anywhere.\n"
                f"    A typo, or prose that reads like a call. If it is prose, add it\n"
                f"    to EXTRA_NON_TOOL_TERMS; if it is a new harness tool, add it to\n"
                f"    HARNESS_TOOLS."
            )

    # Skills are behaviour: loaded into context at runtime, and they name tools
    # the same way. Which agent loads which skill is not checked -- the
    # orchestrator lists "/skills/", so the union is every tool regardless.
    for skill in sorted((root / "workspace" / "skills").glob("*/SKILL.md")):
        named = set(TOKEN.findall(skill.read_text(encoding="utf-8")))
        named_anywhere |= named
        unknown = sorted(named - bound - benign)
        if unknown:
            rel = skill.relative_to(root)
            problems.append(f"  {rel}: names {', '.join(unknown)}, which is not a tool anywhere.")

    missing = sorted(bound - named_anywhere)
    if missing:
        problems.append(
            f"  no prompt or skill names: {', '.join(missing)}.\n"
            f"    These tools are bound but the model is never told they exist.\n"
            f"    Name them somewhere, or drop them from the tool lists."
        )

    ungated = sorted(set(IRREVERSIBLE_TOOLS) - bound)
    if ungated:
        problems.append(
            f"  IRREVERSIBLE_TOOLS names {', '.join(ungated)}, which is not bound.\n"
            f"    INTERRUPT_ON is derived from that list, so the approval gate is\n"
            f"    keyed to a tool that does not exist and the real one runs\n"
            f"    unreviewed. test_every_irreversible_tool_is_gated cannot see this:\n"
            f"    it compares INTERRUPT_ON against the list it was built from."
        )

    if not problems:
        return 0

    print("Prompts and the bound tool surface disagree:\n", file=sys.stderr)
    print("\n\n".join(problems), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
