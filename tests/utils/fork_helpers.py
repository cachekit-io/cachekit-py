"""Helpers for tests that fork with os.fork() and read the child's outcome over a pipe."""

from __future__ import annotations

import ast
import os
import select
import signal
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn


def report(w: int, outcome: object) -> NoReturn:
    """End a child forked with os.fork() with an outcome for its parent; never return into pytest."""
    try:
        os.write(w, repr(outcome).encode())  # literals only: ast.literal_eval reads it
    finally:
        os._exit(0)


def child_outcome(pid: int, r: int, timeout: float = 20.0) -> object:
    """What the child at pid reported on the pipe r, or why it reported nothing; a hung child is killed."""
    if pid <= 0:  # not an assert: python -O strips those, and os.kill(0, ...) signals pytest's whole process group
        raise ValueError("no child was forked")
    ready = select.select([r], [], [], timeout)[0]
    data = os.read(r, 65536) if ready else b""
    if not data:  # on EOF the child has exited (it holds the only write end), and a kill leaves its exit status as is
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(r)
    if data:
        return ast.literal_eval(data.decode())
    if not ready:
        return f"no outcome: timed out after {timeout:g} s"
    if os.WIFSIGNALED(status):
        return f"no outcome: killed by signal {os.WTERMSIG(status)} ({signal.Signals(os.WTERMSIG(status)).name})"
    return f"no outcome: exited {os.WEXITSTATUS(status)}"


def on_new_thread(fn: Callable[[], object], timeout: float = 5.0) -> object:
    """fn's result from a new thread, or "hung" if it holds the thread past timeout."""
    out: list[object] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    return out[0] if out else "hung"


def run_fresh(outcome: Callable[..., object], *args: object) -> subprocess.CompletedProcess[str]:
    """Run outcome(*args) in a fresh interpreter; its stdout is the outcome's repr, for ast.literal_eval.

    For a test that forks from C: pytest's own interpreter has threads it cannot end (pytest-xdist's,
    other tests'), and one waiting for the GIL at such a fork hangs the child in no cachekit code.
    outcome must be a module-level function under `tests`, and args literals.
    """
    code = (
        f"from {outcome.__module__} import {outcome.__name__} as outcome; "
        f"from tests.utils.fork_helpers import print_outcome; print_outcome(outcome, *{args!r})"
    )
    root = Path(__file__).resolve().parents[2]  # where `tests` imports from
    return subprocess.run(  # noqa: S603 - trusted: sys.executable + code built from our own names and literals
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60
    )


def print_outcome(outcome: Callable[..., object], *args: object) -> NoReturn:
    """Print outcome(*args) for run_fresh and end without exit handlers: logging's would wait on a lock a gone thread holds."""
    try:
        print(repr(outcome(*args)), flush=True)
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    os._exit(0)
