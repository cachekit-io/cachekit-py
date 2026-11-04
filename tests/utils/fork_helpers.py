"""Helpers for tests that fork with os.fork() and read the child's outcome over a pipe."""

from __future__ import annotations

import ast
import ctypes
import faulthandler
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn


def report(w: int, outcome: object) -> NoReturn:
    """End a child forked with os.fork() with an outcome for its parent; never return into pytest."""
    try:
        os.write(w, repr(outcome).encode())  # literals only: ast.literal_eval reads it
    finally:
        os._exit(0)


def _has_handler(signum: int) -> bool:
    """Whether a handler is installed for signum; signal.getsignal cannot see one faulthandler.register installed."""
    action = ctypes.create_string_buffer(256)  # holds struct sigaction on Linux and macOS, whose handler comes first
    if ctypes.CDLL(None, use_errno=True).sigaction(signum, None, action):
        raise OSError(ctypes.get_errno(), f"sigaction({signal.Signals(signum).name}) failed")
    return ctypes.c_void_p.from_buffer(action).value not in (None, 1)  # SIG_DFL is 0, SIG_IGN is 1


def _dump_signals() -> tuple[tuple[signal.Signals, bool], ...]:
    """(signal, all_threads) for each dump, in the order a hung child is sent them.

    The thread the child runs, the one that hung, comes first and alone. faulthandler dumps at most 100 thread
    states, newest first, and a child of a fork made from C keeps every parent thread's: in a busy parent the
    all-threads dump alone cuts off the oldest, the main thread that forked and hung.
    """
    return (signal.SIGUSR1, False), (signal.SIGUSR2, True)  # a function: POSIX-only, and this module imports anywhere


def _unregister_dumps() -> None:
    for signum, _ in _dump_signals():
        faulthandler.unregister(signum)


def fork_dumping_stacks(fork: Callable[[], int] | None = None) -> tuple[int, bool]:
    """fork() (os.fork by default), with a child that dumps its stacks to stderr on SIGUSR1 and SIGUSR2: (pid, armed).

    SIGUSR1 dumps the thread the child runs, SIGUSR2 every thread state it holds.
    Armed before the fork, so a child hung in an at-fork hook, before os.fork() returns there, still dumps.
    So does the child of a fork made from C (fork=ctypes.PyDLL(None).fork): faulthandler's handler is C code,
    so it dumps from a child hung where no Python runs, waiting for the GIL included. That child's SIGUSR2 dump also
    shows the other parent threads' frames as the fork froze them, such as one holding a lock the child waits on.
    A SIGUSR1 or SIGUSR2 handler the parent already has is left alone, and the dump stays unarmed. Pass armed to
    child_outcome as dump_stacks, so neither signal reaches a handler this helper did not install.
    """
    fork = fork or os.fork  # looked up here: a default of os.fork would fail this module's import where it is missing
    if any(_has_handler(signum) for signum, _ in _dump_signals()):  # unregister would restore SIG_DFL over it
        warnings.warn(
            "SIGUSR1 or SIGUSR2 already has a handler: a hung child is killed without dumping its stacks", stacklevel=2
        )
        return fork(), False
    try:
        for signum, all_threads in _dump_signals():  # the child inherits them
            faulthandler.register(signum, file=sys.__stderr__, all_threads=all_threads)
        pid = fork()
    except BaseException:
        _unregister_dumps()
        raise
    if pid:
        _unregister_dumps()
    return pid, True


def child_outcome(pid: int, r: int, timeout: float = 20.0, dump_stacks: bool = False) -> object:
    """What the child at pid reported on the pipe r, or why it reported nothing; a hung child is killed.

    dump_stacks: fork_dumping_stacks armed the child's dump, so a hung one is asked for its stacks before the kill.
    """
    if pid <= 0:  # not an assert: python -O strips those, and os.kill(0, ...) signals pytest's whole process group
        raise ValueError("no child was forked")
    deadline = time.monotonic() + timeout
    data, eof = b"", False
    # Done is the child's exit, not EOF: another process can hold the write end, and a child can close it and hang.
    # Reading meanwhile lets a child whose outcome outgrows the pipe's buffer write the rest.
    while (remaining := deadline - time.monotonic()) > 0:
        if not eof and select.select([r], [], [], min(remaining, 0.05))[0]:
            chunk = os.read(r, 65536)
            data, eof = data + chunk, not chunk
            continue
        exited, status = os.waitpid(pid, os.WNOHANG)
        if exited:
            break
        if eof:
            time.sleep(min(remaining, 0.05))
    else:  # the child is still running at the deadline: hung, mid-report or not
        if dump_stacks:
            for signum, _ in _dump_signals():
                os.kill(pid, signum)
                time.sleep(0.5)  # a handler writes its dump in well under this; one signalled mid-dump would skip its own
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(r)
        return f"no outcome: timed out after {timeout:g} s"
    # What the child wrote just before it exited is what the pipe holds now, whatever its capacity. Reading only
    # that much, a process still writing cannot hold us here. Imported here: POSIX-only, as os.fork is.
    import fcntl
    import termios

    left = struct.unpack("i", fcntl.ioctl(r, termios.FIONREAD, b"\0\0\0\0"))[0]
    while left > 0 and (chunk := os.read(r, left)):
        data, left = data + chunk, left - len(chunk)
    os.close(r)
    if data:
        return ast.literal_eval(data.decode())
    if os.WIFSIGNALED(status):
        return f"no outcome: killed by signal {os.WTERMSIG(status)} ({signal.Signals(os.WTERMSIG(status)).name})"
    return f"no outcome: exited {os.WEXITSTATUS(status)}"


def fork_warns() -> bool:
    """Whether os.fork() here warns that the process is multi-threaded (it does from Python 3.12). The child exits at once."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        child = os.fork()
        if child == 0:
            os._exit(0)
    os.waitpid(child, 0)
    return any("multi-threaded" in str(w.message) for w in caught)


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
