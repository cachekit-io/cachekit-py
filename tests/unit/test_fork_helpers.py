"""The fork helpers stay bounded, import everywhere, and never disturb a SIGUSR1 handler their caller has."""

from __future__ import annotations

import ast
import faulthandler
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.utils.fork_helpers import child_outcome, fork_dumping_stacks, report, run_fresh


def _hang_in_child() -> None:
    time.sleep(60)


def _hung_child_of_a_parent_with_its_own_sigusr1_handler() -> object:
    """Fork a child that hangs in an at-fork hook from a parent whose SIGUSR1 already dumps; run only via run_fresh."""
    with tempfile.TemporaryFile() as dump:
        faulthandler.register(signal.SIGUSR1, file=dump, all_threads=True)
        os.register_at_fork(after_in_child=_hang_in_child)  # inside os.fork(): the child never returns from it
        r, w = os.pipe()
        pid, armed = fork_dumping_stacks()
        if pid == 0:
            report(w, "not hung")
        os.close(w)
        child = child_outcome(pid, r, timeout=1, dump_stacks=armed)
        child_signalled = os.fstat(dump.fileno()).st_size > 0  # the child inherited the handler and the file
        os.kill(os.getpid(), signal.SIGUSR1)  # with the registration gone, SIGUSR1's default action ends this process
        dump.seek(0)
        parent_dumped = b"most recent call first" in dump.read()
    return {"armed": armed, "child": child, "child_signalled": child_signalled, "parent_dumped": parent_dumped}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_dumping_stacks_leaves_a_sigusr1_handler_it_did_not_install_alone() -> None:
    """A fresh interpreter: signalling pytest's own process would end the run if the registration were lost."""
    run = run_fresh(_hung_child_of_a_parent_with_its_own_sigusr1_handler)
    assert run.returncode == 0, run.stderr
    assert ast.literal_eval(run.stdout) == {
        "armed": False,
        "child": "no outcome: timed out after 1 s",
        "child_signalled": False,  # a timeout kills an unarmed child without running the parent's handler in it
        "parent_dumped": True,  # the parent's registration survived
    }
    assert "SIGUSR1 already has a handler" in run.stderr


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_child_outcome_reads_an_outcome_larger_than_the_pipe_buffer() -> None:
    big = "x" * (1 << 20)  # a Linux pipe holds 64 KiB: the child blocks writing the rest until the parent reads it
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        report(w, big)
    os.close(w)
    assert child_outcome(pid, r, timeout=10) == big


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_child_outcome_returns_the_outcome_while_another_process_holds_the_write_end() -> None:
    r, w = os.pipe()
    leaked = os.dup(w)  # as a sibling forked meanwhile by another thread, or a grandchild, would hold it
    pid = os.fork()
    if pid == 0:
        os.close(r)
        report(w, True)
    os.close(w)
    try:
        assert child_outcome(pid, r, timeout=5) is True  # no EOF ever comes, but the child exited
    finally:
        os.close(leaked)


def _write_forever(w: int) -> None:
    while True:
        os.write(w, b"x" * 65536)


def _hang_after_reporting(w: int) -> None:
    os.write(w, b"True")
    os.close(w)
    time.sleep(60)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.parametrize("child", [_write_forever, _hang_after_reporting], ids=["writes-forever", "hangs-after-reporting"])
def test_child_outcome_kills_a_child_still_running_at_its_deadline(child: Callable[[int], None]) -> None:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            child(w)
        finally:
            os._exit(1)
    os.close(w)
    started = time.monotonic()
    assert child_outcome(pid, r, timeout=0.5) == "no outcome: timed out after 0.5 s"
    assert time.monotonic() - started < 5  # the deadline bounds reading and reaping alike


def test_fork_helpers_import_where_os_fork_is_missing() -> None:
    """Windows has no os.fork, and a test module there imports these helpers before its skip markers run."""
    code = "import os; os.__dict__.pop('fork', None); import tests.utils.fork_helpers"
    root = Path(__file__).resolve().parents[2]  # where `tests` imports from
    run = subprocess.run(  # noqa: S603 - trusted: sys.executable + a literal
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
