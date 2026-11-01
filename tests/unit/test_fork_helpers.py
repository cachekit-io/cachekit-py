"""The fork helpers must not disturb the pytest process that calls them."""

from __future__ import annotations

import ast
import faulthandler
import os
import signal
import tempfile

import pytest

from tests.utils.fork_helpers import child_outcome, fork_dumping_stacks, report, run_fresh


def _parent_dump_after_fork_dumping_stacks() -> object:
    """What a SIGUSR1 faulthandler registration made before fork_dumping_stacks does after it; run only via run_fresh."""
    with tempfile.TemporaryFile() as dump:
        faulthandler.register(signal.SIGUSR1, file=dump, all_threads=True)
        r, w = os.pipe()
        pid = fork_dumping_stacks()
        if pid == 0:
            report(w, True)
        os.close(w)
        child = child_outcome(pid, r)
        os.kill(os.getpid(), signal.SIGUSR1)  # with the registration gone, SIGUSR1's default action ends this process
        dump.seek(0)
        return {"child": child, "dumped": b"most recent call first" in dump.read()}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_dumping_stacks_keeps_a_registration_the_parent_already_had() -> None:
    """A fresh interpreter: signalling pytest's own process would end the run if the registration were lost."""
    run = run_fresh(_parent_dump_after_fork_dumping_stacks)
    assert run.returncode == 0, run.stderr
    assert ast.literal_eval(run.stdout) == {"child": True, "dumped": True}
    assert "SIGUSR1 already has a handler" in run.stderr
