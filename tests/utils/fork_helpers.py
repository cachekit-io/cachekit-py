"""Helpers for tests that fork with os.fork() and read the child's outcome over a pipe."""

from __future__ import annotations

import ast
import os
import select
import signal
import threading
from collections.abc import Callable
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
