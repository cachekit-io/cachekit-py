"""The instruction-budget PR comment (tests/performance/ir_report.py) rendered from shard reports."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.performance import ir_budget, ir_report
from tests.performance.ir_report import MARKER, load_report, render

RUN = "https://github.com/o/r/actions/runs/1"
JOBS = {
    f"Instruction budget (shard {k}/4)": {
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{k}",
        "conclusion": c,
    }
    for k, c in ((1, "success"), (2, "success"), (3, "success"), (4, "success"))
}


def _report(*rows: tuple[str, str, int | None, int | None, float | None], verdict: str = "PASS") -> dict:
    return {
        "verdict": verdict,
        "interpreter": "cpython-3.12-x86_64",
        "python": "3.12.13",
        "rows": [{"path": p, "verdict": v, "ir": ir, "budget": b, "pct": pct} for p, v, ir, b, pct in rows],
    }


def _jobs(*failed: int) -> dict[str, dict]:
    """JOBS with these shards' jobs ended in failure."""
    names = {f"Instruction budget (shard {k}/4)" for k in failed}
    return {name: job | {"conclusion": "failure"} if name in names else job for name, job in JOBS.items()}


def _all_ok() -> dict[int, dict | None]:
    return {k: _report((f"path_{k}", "ok", 10_100, 10_090, 0.1)) for k in range(1, 5)}


def _rows(body: str) -> dict[str, str]:
    """Table rows keyed by their second cell."""
    return {cells[2].strip(): line for line in body.splitlines() if (cells := line.split("|")) and len(cells) > 3}


def test_all_pass() -> None:
    body = render(_all_ok(), 4, JOBS, "aaa", "bbb", RUN)
    assert body.startswith(MARKER + "\n")
    assert "### Instruction budget: PASS" in body
    assert "Head `aaa` measured against base `bbb`" in body and "`cpython-3.12-x86_64` (3.12.13)" in body
    assert f"[Workflow run]({RUN})" in body
    assert _rows(body)["`path_1`"] == "| ok | `path_1` | 10,100 | 10,090 | +0.10% |"
    assert "<details>" not in body  # every row is visible


def test_a_warn_passes_and_shows_first() -> None:
    reports = _all_ok()
    reports[3] = _report(("file_set", "WARN", 10_031, 10_000, 0.31), ("l1_hit", "ok", 5, 5, 0.0))
    body = render(reports, 4, JOBS, "aaa", "bbb", RUN)
    assert "### Instruction budget: PASS" in body
    assert _rows(body)["`file_set`"] == "| **WARN** | `file_set` | 10,031 | 10,000 | +0.31% |"
    table = [line for line in body.splitlines() if line.startswith("| ")][1:]
    assert "file_set" in table[0]


def test_a_fail_fails_and_a_new_path_has_no_figures() -> None:
    reports = _all_ok()
    reports[2] = _report(("l2_hit", "FAIL", 10_100, 10_000, 1.0), ("new_path", "new", None, None, None), verdict="FAIL")
    body = render(reports, 4, _jobs(2), "aaa", "bbb", RUN)
    assert "### Instruction budget: FAIL" in body
    assert _rows(body)["`l2_hit`"] == "| **FAIL** | `l2_hit` | 10,100 | 10,000 | +1.00% |"
    assert _rows(body)["`new_path`"] == "| new | `new_path` | – | – | – |"


@pytest.mark.parametrize("other", ["PASS", "FAIL"])
def test_a_missing_shard_is_incomplete_and_links_its_job(other: str) -> None:
    reports = _all_ok()
    reports[2] = None
    jobs = _jobs(2)
    if other == "FAIL":
        reports[1] = _report(("l1_hit", "FAIL", 2, 1, 100.0), verdict="FAIL")
        jobs = _jobs(1, 2)
    body = render(reports, 4, jobs, "aaa", "bbb", RUN)
    assert "### Instruction budget: INCOMPLETE" in body
    row = "| **no result** | [Instruction budget (shard 2/4)](https://github.com/o/r/actions/runs/1/job/2) (failure) | | | |"
    assert row in body.splitlines()


@pytest.mark.parametrize(("verdict", "conclusion"), [("PASS", "failure"), ("FAIL", "success"), ("PASS", "cancelled")])
def test_a_report_its_job_did_not_end_with_is_no_result(verdict: str, conclusion: str) -> None:
    """A re-run shard that dies before its upload leaves the earlier attempt's report: an old PASS must not count."""
    reports = _all_ok()
    reports[3] = _report(("l2_hit", "ok", 1, 1, 0.0), verdict=verdict)
    name = "Instruction budget (shard 3/4)"
    body = render(reports, 4, JOBS | {name: JOBS[name] | {"conclusion": conclusion}}, "aaa", "bbb", RUN)
    assert "### Instruction budget: INCOMPLETE" in body
    assert "`l2_hit`" not in body


def test_a_missing_shard_without_a_listed_job_links_the_run() -> None:
    body = render({}, 4, {}, "aaa", "bbb", RUN)
    assert "### Instruction budget: INCOMPLETE" in body
    assert body.count(f"]({RUN}) |") == 4


@pytest.mark.parametrize("text", ["", "{", "[]", "{}", '{"verdict": "WARN", "rows": []}', '{"verdict": "PASS"}'])
def test_a_report_ir_budget_did_not_write_is_no_report(tmp_path, text: str) -> None:
    (tmp_path / "r.json").write_text(text)
    assert load_report(tmp_path / "r.json") is None
    assert load_report(tmp_path / "absent.json") is None


def test_the_footnote_states_the_gate_thresholds() -> None:
    body = render(_all_ok(), 4, JOBS, "aaa", "bbb", RUN)
    assert f"+{ir_budget.FAIL_PCT:g}% or more" in body
    assert set(ir_budget.TOLERANCE_PCT) == {"serializer_orjson"}  # the footnote names no other exception
    assert f"(+{ir_budget.TOLERANCE_PCT['serializer_orjson'][0]:g}% for `serializer_orjson`)" in body


def test_ci_names_the_marker_and_the_shard_jobs() -> None:
    """The comment job finds its comment by this marker, and a missing shard is linked by its job name."""
    ci = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    assert f"marker='{MARKER}'" in ci
    assert "name: " + ir_report.JOB_NAME.format(k="${{ matrix.shard }}", n=4) in ci


def test_the_cli_renders_shard_files(tmp_path) -> None:
    """As CI runs it: system python, one ir-report-K.json per shard (shard 2 left its placeholder), jobs as JSON lines."""
    reports = tmp_path / "reports"
    reports.mkdir()
    for k in (1, 3, 4):
        (reports / f"ir-report-{k}.json").write_text(json.dumps(_report((f"p{k}", "ok", 1, 1, 0.0))))
    (reports / "ir-report-2.json").write_text("{}\n")
    jobs = tmp_path / "jobs.jsonl"
    jobs.write_text("".join(json.dumps({"name": n} | j) + "\n" for n, j in JOBS.items()))
    script = Path(ir_report.__file__)
    args = ["--reports", reports, "--shards", "4", "--jobs", jobs, "--head", "aaa", "--base", "bbb", "--run-url", RUN]
    cmd = [sys.executable, str(script), *map(str, args)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603 (trusted: this repo's script)
    assert out == render({k: load_report(reports / f"ir-report-{k}.json") for k in range(1, 5)}, 4, JOBS, "aaa", "bbb", RUN)
    assert "### Instruction budget: INCOMPLETE" in out and "shard 2/4" in out
