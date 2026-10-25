"""Render the CI instruction-budget shards' ``--report`` files as one pull-request comment.

CI runs ``ir_budget.py --base`` in N shard jobs, each writing a JSON report once it has a verdict over a
placeholder (``{}``). A shard that crashed, hit an environment error or timed out leaves the placeholder or
no report at all, so it shows as "no result", linked to its job, and the overall verdict is INCOMPLETE: a missing shard can hide a regression, so it never reads as PASS.

This runs in a job that installs nothing: standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MARKER = "<!-- cachekit-instruction-budget -->"  # ci.yml's perf-ir-comment edits the bot comment that opens with it
JOB_NAME = "Instruction budget (shard {k}/{n})"  # ci.yml's perf-ir job name
SEVERITY = {"FAIL": 0, "WARN": 1, "new": 2, "LOWER": 3, "ok": 4}  # rows that need a look first
ARTIFACT = "perf-ir-report-{k}"  # ci.yml's name for shard K's uploaded report
CONCLUSION = {"PASS": "success", "FAIL": "failure"}  # the shard job's conclusion each verdict ends it with


def load_report(path: Path) -> dict | None:
    """A shard's report, or None when it is missing or not one ``ir_budget.py --report`` wrote."""
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("verdict") not in ("PASS", "FAIL") or not isinstance(report.get("rows"), list):
        return None
    return report


def _figure(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else "–"


def render(reports: dict[int, dict | None], shards: int, jobs: dict[str, dict], head: str, base: str, run_url: str) -> str:
    """The comment body. ``reports`` maps shard K to its report (None if it has none); ``jobs`` maps job name
    to the job's ``html_url``, ``conclusion`` and ``started_at`` as the GitHub jobs API lists them, plus
    ``report_at``, when its shard's report artifact was uploaded.

    A report counts only if it was uploaded after its job started and its verdict is how that job ended (a
    post-job step can still fail a shard that wrote PASS). Artifacts outlive a re-run, so a re-run
    shard that dies before uploading leaves the earlier attempt's report behind, uploaded before the re-run
    started. A partial re-run lists the shards it skipped with their original start times, so their reports
    still count.
    """

    def current(k: int) -> dict | None:
        job = jobs.get(JOB_NAME.format(k=k, n=shards), {})
        report, started, uploaded = reports.get(k), job.get("started_at"), job.get("report_at")
        if report is None or not (started and uploaded and uploaded >= started):  # same-format UTC timestamps
            return None
        return report if job.get("conclusion") == CONCLUSION[report["verdict"]] else None

    counted = {k: current(k) for k in range(1, shards + 1)}
    missing = [k for k, r in counted.items() if r is None]
    present = [r for r in counted.values() if r is not None]
    rows = sorted((row for r in present for row in r["rows"]), key=lambda row: (SEVERITY.get(row["verdict"], -1), row["path"]))
    if missing:
        overall = "INCOMPLETE"
    elif any(r["verdict"] == "FAIL" for r in present):
        overall = "FAIL"
    else:
        overall = "PASS"
    interpreters = sorted({f"`{r['interpreter']}` ({r['python']})" for r in present})

    out = [
        MARKER,
        f"### Instruction budget: {overall}",
        "",
        f"Head `{head}` measured against base `{base}` on the same runner"
        + (f", on {', '.join(interpreters)}" if interpreters else "")
        + f". [Workflow run]({run_url})",
        "",
        "| Verdict | Path | Head Ir/op | Base Ir/op | Delta |",
        "|---|---|--:|--:|--:|",
    ]
    for k in missing:
        name = JOB_NAME.format(k=k, n=shards)
        job = jobs.get(name, {})
        why = f" ({job['conclusion']})" if job.get("conclusion") else ""
        out.append(f"| **no result** | [{name}]({job.get('html_url') or run_url}){why} | | | |")
    for row in rows:
        verdict = f"**{row['verdict']}**" if row["verdict"] in ("FAIL", "WARN") else row["verdict"]
        pct = f"{row['pct']:+.2f}%" if isinstance(row.get("pct"), (int, float)) else "–"
        out.append(f"| {verdict} | `{row['path']}` | {_figure(row.get('ir'))} | {_figure(row.get('budget'))} | {pct} |")
    out += [
        "",
        "A path fails at +1% or more against the base (+2% for `serializer_orjson`). This check is not required: "
        "a deliberate cost increase is accepted at merge. Method: `docs/performance.md`, *Instruction budgets*.",
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reports", type=Path, required=True, help="directory holding each shard's ir-report-K.json")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--jobs", type=Path, required=True, help="this run's jobs, one JSON object per line")
    parser.add_argument("--artifacts", type=Path, required=True, help="this run's artifacts, one JSON object per line")
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    reports = {k: load_report(args.reports / f"ir-report-{k}.json") for k in range(1, args.shards + 1)}
    jobs = {job["name"]: job for job in map(json.loads, args.jobs.read_text().splitlines()) if job}
    uploaded = {a["name"]: a["created_at"] for a in map(json.loads, args.artifacts.read_text().splitlines()) if a}
    for k in range(1, args.shards + 1):
        if (job := jobs.get(JOB_NAME.format(k=k, n=args.shards))) and (at := uploaded.get(ARTIFACT.format(k=k))):
            job["report_at"] = at
    sys.stdout.write(render(reports, args.shards, jobs, args.head, args.base, args.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
