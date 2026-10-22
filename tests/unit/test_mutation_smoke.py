"""The mutation smoke's runner: what counts as a kill, when a mutant applies, and that the workflow runs them all."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("mutation_smoke", ROOT / "scripts" / "mutation_smoke.py")
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

MUTANT = smoke.Mutant("m", "f.py", "old", "new", ("tests/unit/test_x.py::TestC::test_k", "tests/unit/test_x.py::test_j[a-b]"))
KILLER = ("tests.unit.test_x.TestC", "test_k")


@pytest.mark.parametrize("rc", [0, 2, 3, 4, 5, None])
def test_only_exit_1_can_kill(rc):
    """Collection errors, interrupts, usage errors, no tests and timeouts never count, even with a killer failed."""
    killed, message = smoke.verdict(MUTANT, rc, {KILLER: "failed"})
    assert not killed
    assert message.startswith("SURVIVED m:")


def test_exit_1_without_a_killer_failure_survives():
    killed, _ = smoke.verdict(MUTANT, 1, {KILLER: "passed", ("tests.unit.test_x", "test_other"): "failed"})
    assert not killed


def test_exit_1_with_a_killer_failure_kills():
    killed, message = smoke.verdict(MUTANT, 1, {("tests.unit.test_x", "test_j[a-b]"): "failed"})
    assert killed
    assert message == "KILLED m by tests/unit/test_x.py::test_j[a-b]"


@pytest.mark.parametrize("source", ["nothing here", "old and old"])
def test_anchor_must_match_exactly_once(source):
    with pytest.raises(smoke.NotAppliedError, match="anchor matches"):
        smoke.mutated_source(MUTANT, source)


def test_anchor_is_replaced():
    assert smoke.mutated_source(MUTANT, "a old b") == "a new b"


def test_junit_key_matches_pytest_junit_naming():
    assert smoke.junit_key("tests/unit/backends/test_f.py::TestE::test_s") == ("tests.unit.backends.test_f.TestE", "test_s")
    assert smoke.junit_key("tests/unit/test_f.py::test_m[get_buffer-None]") == ("tests.unit.test_f", "test_m[get_buffer-None]")


def test_junit_outcomes(tmp_path):
    xml = tmp_path / "junit.xml"
    xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="c" name="passed"/>'
        '<testcase classname="c" name="failed"><failure message="x"/></testcase>'
        '<testcase classname="c" name="errored"><error message="x"/></testcase>'
        '<testcase classname="c" name="skipped"><skipped message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    assert smoke.junit_outcomes(xml) == {
        ("c", "passed"): "passed",
        ("c", "failed"): "failed",
        ("c", "errored"): "error",
        ("c", "skipped"): "skipped",
    }


def test_a_killer_erroring_in_setup_is_not_a_kill():
    """A setup or teardown error is an infrastructure failure, not the mutant being caught."""
    killed, _ = smoke.verdict(MUTANT, 1, {KILLER: "error"})
    assert not killed


@pytest.mark.parametrize("mutant", smoke.MUTANTS, ids=lambda m: m.name)
def test_every_anchor_still_applies(mutant):
    """A refactor that breaks an anchor fails here, on its own PR, not at the next nightly. Re-anchor the mutant."""
    smoke.mutated_source(mutant, (ROOT / mutant.path).read_text())


def test_every_mutant_records_killers_and_has_a_unique_name():
    assert all(m.killers for m in smoke.MUTANTS)
    assert len(smoke.BY_NAME) == len(smoke.MUTANTS)


def test_workflow_matrix_runs_the_baseline_and_every_mutant():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "mutation-smoke.yml").read_text())
    legs = workflow["jobs"]["smoke"]["strategy"]["matrix"]["mutant"]
    assert legs == ["baseline", *smoke.BY_NAME]
