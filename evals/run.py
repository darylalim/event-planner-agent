"""Run the evaluation suites, and compare two result files.

**`--suite` spends money.** It calls the live model on your `ANTHROPIC_API_KEY`
and refuses to run without `--yes-spend` and a `--max-usd` cap. Tavily is never
called: web results come from each scenario's fixtures. `compare` and `rescore`
read files and spend nothing.

    uv run python -m evals.run --suite smoke --reps 1 --yes-spend --max-usd 5
    uv run python -m evals.run --suite full --reps 3 --yes-spend --max-usd 30
    uv run python -m evals.run compare evals/results/A.jsonl evals/results/B.jsonl
    uv run python -m evals.run rescore evals/results/A.jsonl

Run `smoke` after a prompt or middleware change, and `full` before changing a
model or an effort level, against a baseline run the same day. `rescore` re-runs
the checks over saved captures, so fixing a check does not mean paying for the
model again.

The cap is approximate: it is read between runs, and one run has reached $2.50,
so set it with headroom.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.checks import GATES, SCORED, Outcome, evaluate
from evals.driver import Capture, run_scenario
from evals.scenarios import BY_NAME, Scenario, suite
from event_planner.cli import _load_env, credentials_problem
from event_planner.models import ORCHESTRATOR_MODEL
from event_planner.subagents import SUBAGENT_MODELS

RESULTS = Path(__file__).resolve().parent / "results"

#: A scored check has regressed when it drops by more than one run in three.
#: The epsilon keeps exactly one in three, which floats land either side of, a pass.
REGRESSION = 1 / 3 + 1e-9

#: `compare` warns past this, since a baseline is meant to be from the same day.
STALE_BASELINE_DAYS = 1


def set_tracing(enabled: bool) -> str:
    """Pin tracing on or off and say which.

    `.env.example` turns it on, so left alone a run would upload. TRACING_V2 is
    read before LANGSMITH_TRACING in both namespaces, and `load_dotenv` never
    overrides a variable already set, so this holds whatever `.env` says as long
    as it runs before `_load_env`.
    """
    os.environ["LANGSMITH_TRACING_V2"] = "true" if enabled else "false"
    return "on: this run uploads to LangSmith" if enabled else "off"


def roster() -> dict[str, str]:
    choices = {"orchestrator": ORCHESTRATOR_MODEL, **SUBAGENT_MODELS}
    return {role: f"{c.model}@{c.effort}" for role, c in choices.items()}


def _git_head() -> str:
    try:
        done = subprocess.run(
            ["git", "describe", "--always", "--dirty"],  # noqa: S607 - git from PATH
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip()


def _outcomes_json(outcomes: dict[str, Outcome]) -> dict[str, dict[str, str]]:
    return {name: dataclasses.asdict(outcome) for name, outcome in outcomes.items()}


def run_record(capture: Capture, scenario: Scenario) -> dict[str, Any]:
    return {
        "kind": "run",
        "capture": capture.to_json(),
        "outcomes": _outcomes_json(evaluate(capture, scenario.expect)),
    }


def _line(path: Path, record: dict[str, Any]) -> None:
    """Append one record, flushed, so a killed run keeps what it already paid for."""
    with path.open("a") as out:
        out.write(json.dumps(record, default=str) + "\n")


def _print_run(capture: Capture, outcomes: dict[str, Outcome], spent: float) -> None:
    failed = [n for n in GATES if outcomes[n].status == "fail"]
    missed = [n for n in SCORED if outcomes[n].status == "fail"]
    unknown = [n for n, o in outcomes.items() if o.status == "unknown"]
    cost = capture.cost()
    print(
        f"{capture.scenario:30} rep {capture.rep}  {capture.wall_clock:5.0f}s  "
        + (f"${cost:.2f}" if cost is not None else "$?")
        + f"  (spent ${spent:.2f})"
    )
    for label, names in (("GATE FAILED", failed), ("scored fail", missed), ("unknown", unknown)):
        for name in names:
            print(f"    {label}: {name}: {outcomes[name].detail}")


def run_suite(
    scenarios: Iterable[Scenario],
    *,
    reps: int,
    max_usd: float,
    out: Path,
    db_dir: Path,
    header: dict[str, Any],
) -> bool:
    """Run every scenario `reps` times, stopping at the cap. True when complete.

    Repetitions are the outer loop, so a stopped run still has a whole first pass
    across the scenarios rather than three of the first few.
    """
    _line(out, {"kind": "header", **header})
    spent = 0.0
    planned = [(rep, scenario) for rep in range(reps) for scenario in scenarios]
    stopped: str | None = None
    finished = 0
    for rep, scenario in planned:
        if spent >= max_usd:
            stopped = f"reached --max-usd {max_usd:.2f}"
            break
        capture = run_scenario(scenario, db_dir=db_dir, rep=rep)
        record = run_record(capture, scenario)
        _line(out, record)
        finished += 1
        cost = capture.cost()
        if cost is None:
            # A cap that cannot measure has to stop rather than guess.
            stopped = f"no price for a model in {capture.scenario}: add it to PRICES"
            break
        spent += cost
        _print_run(capture, evaluate(capture, scenario.expect), spent)

    complete = stopped is None
    _line(
        out,
        {
            "kind": "footer",
            "complete": complete,
            "stopped": stopped,
            "planned": len(planned),
            "finished": finished,
            "spent": round(spent, 4),
        },
    )
    if stopped:
        print(f"\nINCOMPLETE after {finished} of {len(planned)} runs: {stopped}")
    print(f"\nspent about ${spent:.2f}. Results: {out}")
    return complete


# --------------------------------------------------------------------------- #
# reading results
# --------------------------------------------------------------------------- #


@dataclass
class Rate:
    """How one check went across repetitions."""

    passed: int = 0
    failed: int = 0
    unknown: int = 0

    def add(self, status: str) -> None:
        if status == "pass":
            self.passed += 1
        elif status == "fail":
            self.failed += 1
        elif status == "unknown":
            self.unknown += 1

    @property
    def decided(self) -> int:
        return self.passed + self.failed

    @property
    def rate(self) -> float | None:
        return self.passed / self.decided if self.decided else None

    def __str__(self) -> str:
        unknown = f" ({self.unknown} unknown)" if self.unknown else ""
        return f"{self.passed}/{self.decided}{unknown}"


@dataclass
class Results:
    header: dict[str, Any] = field(default_factory=dict)
    footer: dict[str, Any] | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.footer and self.footer.get("complete"))

    def rates(self) -> dict[tuple[str, str], Rate]:
        """(scenario, check) -> rate, skipping checks that did not apply."""
        rates: dict[tuple[str, str], Rate] = defaultdict(Rate)
        for run in self.runs:
            scenario = run["capture"]["scenario"]
            for check, outcome in run["outcomes"].items():
                if outcome["status"] != "na":
                    rates[scenario, check].add(outcome["status"])
        return dict(rates)

    def cost_by_role(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for run in self.runs:
            capture = Capture.from_json(run["capture"])
            for row in capture.usage:
                one = dataclasses.replace(capture, usage=[row])
                totals[row["role"]] += one.cost() or 0.0
        return dict(totals)

    def wall_clock(self) -> float:
        return sum(run["capture"]["wall_clock"] for run in self.runs)


def load(path: Path) -> Results:
    results = Results()
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record["kind"] == "header":
            results.header = record
        elif record["kind"] == "footer":
            results.footer = record
        else:
            results.runs.append(record)
    return results


def summary(results: Results) -> str:
    """Pass rates per check across the file, and any gate that did not pass."""
    totals: dict[str, Rate] = defaultdict(Rate)
    for (_, check), rate in results.rates().items():
        total = totals[check]
        total.passed += rate.passed
        total.failed += rate.failed
        total.unknown += rate.unknown
    lines = [f"{len(results.runs)} runs" + ("" if results.complete else "  INCOMPLETE")]
    for label, names in (("gates", GATES), ("scored", SCORED)):
        lines.append(f"\n{label}:")
        lines += [f"  {name:28} {totals[name]}" for name in names if name in totals]
    lines.append("\ncost by role:")
    costs = results.cost_by_role()
    lines += [f"  {role:28} ${cost:.2f}" for role, cost in sorted(costs.items())]
    lines.append(f"  {'TOTAL':28} ${sum(costs.values()):.2f}, {results.wall_clock():.0f}s")
    return "\n".join(lines)


def _baseline_warnings(base: Results, candidate: Results) -> list[str]:
    warnings = []
    days = _days_apart(base.header.get("stamp"), candidate.header.get("stamp"))
    if days is None or days > STALE_BASELINE_DAYS:
        warnings.append(
            f"baseline and candidate are {days if days is not None else '?'} days apart: "
            "compare against a fresh baseline"
        )
    only = {s for s, _ in base.rates()} ^ {s for s, _ in candidate.rates()}
    if only:
        warnings.append(f"scenarios not in both files: {sorted(only)}")
    if not base.complete:
        warnings.append("the baseline is incomplete")
    for label, results in (("baseline", base), ("candidate", candidate)):
        if results.header.get("degraded"):
            warnings.append(f"the {label} is deliberately degraded output from evals.weak")
    return warnings


def _days_apart(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    spec = "%Y%m%dT%H%M%SZ"
    delta = datetime.strptime(right, spec).replace(tzinfo=UTC) - datetime.strptime(
        left, spec
    ).replace(tzinfo=UTC)
    return abs(delta.days)


def compare(base: Results, candidate: Results) -> tuple[str, list[str]]:
    """Decide on a candidate: `blocked`, `regressed`, `inconclusive` or `clear`.

    A gate failure blocks however good the rest is. A gate that never reached a
    decision is inconclusive rather than a pass, as is a candidate that did not
    finish: neither has shown what the suite exists to show.
    """
    lines = [f"warning: {w}" for w in _baseline_warnings(base, candidate)]
    before, after = base.rates(), candidate.rates()

    gate_failures = [
        f"GATE {check} on {scenario}: {rate}"
        for (scenario, check), rate in sorted(after.items())
        if check in GATES and rate.failed
    ]
    gate_unknown = [
        f"gate undecided {check} on {scenario}: {rate}"
        for (scenario, check), rate in sorted(after.items())
        if check in GATES and rate.unknown and not rate.failed
    ]
    regressions = []
    for key, rate in sorted(after.items()):
        scenario, check = key
        old = before.get(key)
        if check not in SCORED or old is None or old.rate is None or rate.rate is None:
            continue
        if old.rate - rate.rate > REGRESSION:
            regressions.append(f"REGRESSED {check} on {scenario}: {old} -> {rate}")
    lines += gate_failures + gate_unknown + regressions

    if not candidate.complete:
        lines.append("candidate is incomplete")

    lines.append("\ncost by role (baseline -> candidate):")
    old_cost, new_cost = base.cost_by_role(), candidate.cost_by_role()
    for role in sorted(old_cost.keys() | new_cost.keys()):
        lines.append(f"  {role:28} ${old_cost.get(role, 0):.2f} -> ${new_cost.get(role, 0):.2f}")
    lines.append(f"  {'TOTAL':28} ${sum(old_cost.values()):.2f} -> ${sum(new_cost.values()):.2f}")
    lines.append(f"  {'wall clock':28} {base.wall_clock():.0f}s -> {candidate.wall_clock():.0f}s")

    if gate_failures:
        verdict = "blocked"
    elif regressions:
        verdict = "regressed"
    elif gate_unknown or not candidate.complete:
        verdict = "inconclusive"
    else:
        verdict = "clear"
    return verdict, lines


def rescore(path: Path) -> Path:
    """Re-run the checks over saved captures, into `<name>.rescored.jsonl`."""
    results = load(path)
    target = path.with_suffix(".rescored.jsonl")
    target.unlink(missing_ok=True)
    _line(target, {"kind": "header", **results.header})
    for run in results.runs:
        capture = Capture.from_json(run["capture"])
        _line(target, run_record(capture, BY_NAME[capture.scenario]))
    if results.footer:
        _line(target, results.footer)
    return target


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #


def _compare_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.run compare")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    verdict, lines = compare(load(args.baseline), load(args.candidate))
    print("\n".join(lines))
    print(f"\nverdict: {verdict}")
    return 0 if verdict == "clear" else 1


def _rescore_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.run rescore")
    parser.add_argument("results", type=Path)
    args = parser.parse_args(argv)
    target = rescore(args.results)
    print(summary(load(target)))
    print(f"\nrescored: {target}")
    return 0


def _suite_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", choices=("smoke", "full"), required=True)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--scenario", action="append", help="Only this one; repeatable.")
    parser.add_argument("--max-usd", type=float, help="Required with --yes-spend.")
    parser.add_argument("--yes-spend", action="store_true", help="Required: this bills the API.")
    parser.add_argument("--trace", action="store_true", help="Upload to LangSmith. Default: off.")
    parser.add_argument("--out", type=Path, help="Default: evals/results/<stamp>-<suite>.jsonl")
    parser.add_argument("--db-dir", type=Path, help="Default: under the system temp dir.")
    args = parser.parse_args(argv)

    scenarios = suite(args.suite)
    if args.scenario:
        unknown = sorted(set(args.scenario) - set(BY_NAME))
        if unknown:
            parser.error(f"unknown scenarios: {unknown}. Known: {sorted(BY_NAME)}")
        scenarios = tuple(BY_NAME[name] for name in args.scenario)

    if not args.yes_spend or args.max_usd is None:
        print(
            f"`{args.suite}` calls the live model for {len(scenarios) * args.reps} runs. "
            "Re-run with --yes-spend and --max-usd."
        )
        return 2

    tracing = set_tracing(args.trace)
    _load_env()
    if (problem := credentials_problem()) is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or RESULTS / f"{stamp}-{args.suite}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    db_dir = args.db_dir or Path(tempfile.gettempdir()) / "event-planner-evals" / stamp
    print(f"tracing: {tracing}\ndatabases: {db_dir}\nroster: {roster()}\n")
    header = {
        "stamp": stamp,
        "suite": args.suite,
        "reps": args.reps,
        "git": _git_head(),
        "roster": roster(),
        "scenarios": [s.name for s in scenarios],
        "max_usd": args.max_usd,
    }
    complete = run_suite(
        scenarios, reps=args.reps, max_usd=args.max_usd, out=out, db_dir=db_dir, header=header
    )
    print("\n" + summary(load(out)))
    return 0 if complete else 1


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["compare"]:
        return _compare_main(argv[1:])
    if argv[:1] == ["rescore"]:
        return _rescore_main(argv[1:])
    return _suite_main(argv)


if __name__ == "__main__":
    sys.exit(main())
