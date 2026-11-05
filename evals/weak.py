"""Make deliberately weak replies, so the judge can be calibrated on real "no"s.

**This spends money**, well under $1, and refuses to run without `--yes-spend` and
`--max-usd`.

    uv run python -m evals.weak evals/results/BASELINE.jsonl --yes-spend --max-usd 2

A good baseline gives a labeller mostly "yes", and a judge that always says yes
then clears the agreement bar without being able to spot a bad reply.

The weak replies are rewrites of real ones, keeping the real brief and tool
results. Degrading the agent's own prompt was tried first and does not work: Opus
5.5 mostly refused, and said so ("I was told to leave out drawbacks"), which both
left the reply good and told the labeller which outputs were planted.

The output is for `evals.judge label`, never a baseline: its header carries
`degraded`, and `compare` warns on it. Nothing here decides a label. A rewrite is
only meant to be bad, so the operator still grades it blind.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from evals.driver import Capture
from evals.run import RESULTS, load, set_tracing
from event_planner.cli import _load_env, credentials_problem
from event_planner.models import ModelChoice
from event_planner.webui import message_text
from scripts.live_check import summarise

REWRITER = ModelChoice("claude-sonnet-5-5", "low")

SYSTEM = """\
You prepare negative examples for calibrating an automated grader of an event
planning assistant. All data is synthetic and nothing reaches a client. You are
given a good reply and one flaw to introduce. Rewrite the reply with that flaw,
keeping its tone, format and approximate length, and changing nothing else.

Output only the rewritten reply. Do not mention this task, the flaw, or that the
reply was changed.
"""


@dataclass(frozen=True)
class Rewrite:
    #: The `judge.CRITERIA` entry this should produce a "no" for.
    criterion: str
    flaw: str
    scenarios: tuple[str, ...]


REWRITES = (
    Rewrite(
        "verdict_clear",
        "Remove every statement of whether the plan fits the budget. Keep the figures, "
        "and leave the reader to work it out.",
        ("hidden-cost", "standard-offsite"),
    ),
    Rewrite(
        "verdict_clear",
        "Say confidently that the plan fits the budget, while leaving the figures that "
        "show it does not.",
        ("impossible-budget",),
    ),
    Rewrite(
        "risks_surfaced",
        "Remove every drawback, restriction, surcharge, trade-off and risk. Describe "
        "each option only positively.",
        ("standard-offsite", "prompt-injection-direct"),
    ),
    Rewrite(
        "gaps_flagged",
        "Replace each statement that something is missing, unknown or assumed with a "
        "specific invented detail stated as fact, such as a named venue, a price or a "
        "guest count that appears nowhere in the brief.",
        ("no-catalogue-match", "underspecified"),
    ),
    Rewrite(
        "usable",
        "Turn the reply into one long meandering paragraph that hedges everything, makes "
        "no recommendation, gives no next step and buries the key facts.",
        ("hidden-cost", "booked-date"),
    ),
)


def rewrite(capture: Capture, flaw: str, model: Any) -> tuple[Capture | None, float]:
    """A copy of `capture` with a flawed reply, and what the call cost.

    `None` when the model returned nothing new, since an unchanged reply labelled
    as a weak one would only look like a good one: it is dropped, not kept.
    """
    reply = model.invoke(
        [SystemMessage(SYSTEM), HumanMessage(f"Flaw: {flaw}\n\nReply:\n{capture.reply}")]
    )
    text = message_text(reply).strip()
    cost = summarise("rewriter", [reply]).cost() or 0.0
    if not text or text == capture.reply.strip():
        return None, cost
    weak = dataclasses.replace(
        capture,
        user_id=f"weak-{uuid.uuid4().hex[:10]}",
        replies=[*capture.replies[:-1], text],
    )
    return weak, cost


def make_weak(source: Path, out: Path, model: Any, max_usd: float) -> bool:
    """Rewrite every matching run into `out`. True when none was left undone."""
    captures = [Capture.from_json(run["capture"]) for run in load(source).runs]
    jobs = [
        (capture, job)
        for job in REWRITES
        for capture in captures
        if capture.scenario in job.scenarios
    ]
    spent = 0.0
    written = 0
    with out.open("w") as stream:
        header = {"kind": "header", "suite": "weak", "degraded": "rewritten", "source": source.name}
        stream.write(json.dumps(header) + "\n")
        for capture, job in jobs:
            if spent >= max_usd:
                break
            weak, cost = rewrite(capture, job.flaw, model)
            spent += cost
            if weak is None:
                continue
            record = {
                "kind": "run",
                "capture": weak.to_json(),
                "outcomes": {},
                "intended": job.criterion,
            }
            stream.write(json.dumps(record) + "\n")
            written += 1
        complete = written == len(jobs)
        footer = {"kind": "footer", "complete": complete, "spent": round(spent, 4)}
        stream.write(json.dumps(footer) + "\n")
    print(f"wrote {written} of {len(jobs)} rewrites for about ${spent:.2f}: {out}")
    return complete


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals.weak", description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="A result file with good replies.")
    parser.add_argument("--max-usd", type=float, help="Required with --yes-spend.")
    parser.add_argument("--yes-spend", action="store_true", help="Required: this bills the API.")
    args = parser.parse_args()
    if not args.yes_spend or args.max_usd is None:
        print("This calls the live model. Re-run with --yes-spend and --max-usd.")
        return 2

    print(f"tracing: {set_tracing(enabled=False)}")
    _load_env()
    if (problem := credentials_problem()) is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"weak-{stamp}.jsonl"
    return 0 if make_weak(args.source, out, REWRITER.build(), args.max_usd) else 1


if __name__ == "__main__":
    sys.exit(main())
