"""The fork helpers must not disturb a SIGUSR1 handler the process calling them already has."""

from __future__ import annotations

import ast
import faulthandler
import os
import signal
import tempfile
import time

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
