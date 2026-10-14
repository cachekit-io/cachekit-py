"""Unit tests for structured logging module."""

from __future__ import annotations

import ast
import json
import logging
import os
import signal
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from cachekit.logging import (
    StructuredLogger,
    get_structured_logger,
)
from tests.utils.fork_helpers import child_outcome, report, run_fresh


class _Capture(logging.Handler):
    """Collects the JSON entries written to ``cachekit.logging``."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append(json.loads(record.getMessage()))


@pytest.fixture
def written():
    """Entries ``cachekit.logging`` writes while the test runs, with DEBUG enabled."""
    sink = logging.getLogger("cachekit.logging")
    capture, level = _Capture(), sink.level
    sink.addHandler(capture)
    sink.setLevel(logging.DEBUG)
    try:
        yield capture.entries
    finally:
        sink.removeHandler(capture)
        sink.setLevel(level)


def _c_fork_child_outcome() -> object:
    """What a child forked from C reports; run only in a fresh interpreter (TestEntriesAfterFork's C-fork test).

    Importing cachekit must leave this interpreter single-threaded: a thread waiting for the GIL at a
    fork made from C leaves the child hung at its first GIL release, in no cachekit code. logging's
    module lock, the handler's lock and cachekit's metrics lock end held by threads the child lacks,
    and no level check is cached: in the child, any call that takes one of them hangs, as no
    after-fork reset ran there.
    """
    import ctypes
    import faulthandler

    import cachekit  # noqa: F401 - everything an application's import starts
    from cachekit.reliability import metrics_collection

    before = get_structured_logger("test.c_fork.parent")  # built pre-fork, as a module-level _logger is
    before._should_sample = lambda: True  # never sampled away
    capture = _Capture()
    for sink in (logging.getLogger("cachekit.logging"), before.logger):  # the JSON sink and the plain-text one
        sink.addHandler(capture)
        sink.setLevel(logging.DEBUG)
    for lock in (logging._lock, capture.lock, metrics_collection._metrics_lock()):  # held at fork by a thread the child lacks
        holder = threading.Thread(target=lock.acquire)  # ends holding it
        holder.start()
        holder.join()
    if threading.active_count() != 1:  # not an assert: python -O strips those
        raise RuntimeError(f"a thread would be alive at fork: {threading.enumerate()}")
    faulthandler.register(signal.SIGALRM, chain=True)  # the child inherits it: a hang dumps its stack
    libc_fork = ctypes.PyDLL(None).fork  # PyDLL keeps the GIL through the call
    r, w = os.pipe()
    child = libc_fork()
    if child == 0:
        try:
            signal.alarm(5)
            os.close(r)
            requested: list[str] = []

            class _RecordingThread(threading.Thread):  # keeps the child single-threaded
                def start(self) -> None:
                    requested.append(self.name)

            threading.Thread = _RecordingThread  # the child's copy only; it never returns
            before.debug("child debug")
            before.error("child error")
            before.cache_operation("get", "ns:tenant-42:func:app.f:args:x", hit=True)
            hits = metrics_collection.cache_operations.get(
                {"operation": "get", "status": "hit", "serializer": "", "namespace": "default"}
            )
            outcome: object = {"requested": requested, "written": capture.entries, "hits_counted": hits}
        except BaseException as e:
            outcome = {"error": repr(e)}
        try:
            report(w, outcome)
        finally:
            os._exit(1)
    os.close(w)
    return child_outcome(child, r)


def _cycle() -> list:
    cycle: list = ["ns:tenant-42:func:app.f:args:x"]
    cycle.append(cycle)
    return cycle


def _too_deep() -> list:
    nest: list = ["tenant-42"]
    for _ in range(sys.getrecursionlimit() + 100):
        nest = [nest]
    return nest


class TestStructuredLogger:
    """Test StructuredLogger functionality."""

    @pytest.fixture
    def logger(self):
        """Create a test logger instance."""
        return StructuredLogger("test_logger")

    def test_get_context(self, logger):
        """Test context generation."""
        context = logger._get_context()
        assert set(context) == {"timestamp", "thread_id"}
        assert isinstance(context["timestamp"], float)
        assert isinstance(context["thread_id"], int)

    def test_cache_key_always_redacted(self, logger):
        """cache_operation always redacts the key via digest (CWE-532, LAB-304)."""
        from unittest.mock import patch as _patch

        from cachekit.hash_utils import redact_cache_key

        sensitive = "ns:tenant-42:func:app.f:args:email@test.com:v1"
        with _patch("cachekit.logging.logging.Logger.log") as mock_log:
            logger.cache_operation("get", sensitive, hit=True)
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["cache_key"] == redact_cache_key(sensitive)
        assert sensitive not in str(extra)

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_operation_logging(self, mock_log, logger):
        """Test cache operation logging."""
        logger.cache_operation(
            "get",
            "user:email@test.com",
            namespace="users",
            serializer="orjson",
            duration_ms=1.5,
            hit=True,
        )

        mock_log.assert_called_once()
        call_args = mock_log.call_args

        # Check log level
        assert call_args[0][0] == logging.INFO
        assert call_args[0][1] == "cache_operation"

        # Check structured context
        extra = call_args[1]["extra"]["structured"]
        assert extra["operation"] == "get"
        from cachekit.hash_utils import redact_cache_key

        assert extra["cache_key"] == redact_cache_key("user:email@test.com")  # Redacted digest (CWE-532)
        assert extra["namespace"] == "users"
        assert extra["serializer"] == "orjson"
        assert extra["duration_ms"] == 1.5
        assert extra["hit"] is True

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_operation_error_logging(self, mock_log, logger):
        """Test error logging."""
        logger.cache_operation("set", "key123", error="Connection timeout", error_type="TimeoutError")

        mock_log.assert_called_once()
        call_args = mock_log.call_args

        # Should log as ERROR
        assert call_args[0][0] == logging.ERROR

        # Check error context
        extra = call_args[1]["extra"]["structured"]
        assert extra["error"] == "Connection timeout"
        assert extra["error_type"] == "TimeoutError"


class TestSynchronousEntries:
    """Entries are written on the caller's thread: no writer thread, so none to lose after a fork."""

    def test_error_is_written_before_returning_with_this_pid(self, written):
        StructuredLogger("test.sync").error("boom", attempt=2)

        assert len(written) == 1
        entry = written[0]
        assert (entry["level"], entry["message"], entry["logger"], entry["attempt"]) == ("ERROR", "boom", "test.sync", 2)
        assert entry["pid"] == os.getpid()

    def test_pii_named_fields_are_masked(self, written):
        StructuredLogger("test.mask").error("boom", api_token=12345, attempt=1)

        assert written[0]["api_token"] == "****"
        assert written[0]["attempt"] == 1

    def test_unencodable_value_is_written_as_its_type_only(self, written):
        """json cannot encode it; its str() would print the key it carries (CWE-532), so only its type is written."""
        StructuredLogger("test.unencodable").error("boom", failure=ValueError("ns:tenant-42:func:app.f:args:x"))

        assert written[0]["failure"] == "<ValueError>"
        assert "tenant-42" not in json.dumps(written)

    @pytest.mark.parametrize(
        ("build", "type_name"),
        [
            (lambda: {(1, 2): "ns:tenant-42:func:app.f:args:x"}, "<dict>"),
            (_cycle, "<list>"),
        ],
        ids=["tuple-key", "cycle"],
    )
    def test_field_json_raises_on_is_written_as_its_type_only(self, written, build, type_name):
        """json raises on it (TypeError, ValueError): the entry is still written, its other fields intact.

        Raising instead would replace the exception a caller is logging from inside its except block.
        """
        StructuredLogger("test.unwalkable").error("boom", detail=build(), attempt=1)

        assert (written[0]["message"], written[0]["detail"], written[0]["attempt"]) == ("boom", type_name, 1)
        assert "tenant-42" not in json.dumps(written)

    def test_field_nested_past_the_recursion_limit_is_written_as_its_type_only(self, monkeypatch, written):
        """RecursionError, forced through json's pure-Python encoder, whose limit is sys.getrecursionlimit() everywhere.

        The C encoder's limit is the thread's stack: on an unlimited stack, 3.14 encodes a 100_000-deep nest.
        """
        monkeypatch.setattr(json.encoder, "c_make_encoder", None)

        StructuredLogger("test.too_deep").error("boom", detail=_too_deep(), attempt=1)

        assert (written[0]["message"], written[0]["detail"], written[0]["attempt"]) == ("boom", "<list>", 1)

    def test_generated_fields_win_over_same_named_caller_fields(self, written):
        """A caller's pid= cannot make the entry report a process other than the one that wrote it."""
        StructuredLogger("test.reserved").error("boom", pid=1, thread_id=2, logger="spoofed", level="DEBUG")

        entry = written[0]
        assert (entry["pid"], entry["thread_id"]) == (os.getpid(), threading.get_ident())
        assert (entry["logger"], entry["level"]) == ("test.reserved", "ERROR")

    @pytest.mark.parametrize(
        ("level", "sampled", "builds"),
        [(logging.DEBUG, True, True), (logging.DEBUG, False, False), (logging.INFO, True, False)],
        ids=["enabled-sampled", "enabled-sampled-out", "disabled"],
    )
    def test_debug_is_built_only_when_enabled_and_sampled(self, monkeypatch, caplog, level, sampled, builds):
        """Below the logger's level nothing is sampled or serialised: debug call sites cost a level check."""
        caplog.set_level(level, logger="cachekit.logging")
        structured = StructuredLogger("test.gate")
        sample = MagicMock(return_value=sampled)
        write = MagicMock()
        monkeypatch.setattr(structured, "_should_sample", sample)
        monkeypatch.setattr(structured, "_write", write)

        structured.debug("detail", attempt=1)

        assert sample.called is (level == logging.DEBUG)
        assert write.called is builds

    def test_error_and_above_are_never_sampled(self, monkeypatch, written):
        structured = StructuredLogger("test.unsampled")
        monkeypatch.setattr(structured, "_should_sample", lambda: False)

        structured.error("boom")
        structured.log("error", "via log")
        structured.log("CRITICAL", "critical")
        structured.warning("dropped")

        assert [e["message"] for e in written] == ["boom", "via log", "critical"]

    @pytest.mark.parametrize(
        ("name", "levelno"),
        [("warn", logging.WARNING), ("CRITICAL", logging.CRITICAL), ("FATAL", logging.CRITICAL), ("NOTICE", logging.INFO)],
    )
    def test_log_writes_a_level_name_at_its_stdlib_level(self, monkeypatch, caplog, name, levelno):
        """Each stdlib level name maps to its own level, as the old writer's getattr(logging, name) did; others are INFO."""
        caplog.set_level(logging.DEBUG, logger="cachekit.logging")
        structured = StructuredLogger("test.levels")
        monkeypatch.setattr(structured, "_should_sample", lambda: True)

        structured.log(name, "boom")

        assert [(r.levelno, json.loads(r.getMessage())["level"]) for r in caplog.records if r.name == "cachekit.logging"] == [
            (levelno, name.upper())
        ]

    @pytest.mark.parametrize(("draw", "sampled"), [(0.004, True), (0.006, False)])
    def test_sampling_rate_below_one_percent_is_honoured(self, monkeypatch, draw, sampled):
        """A rate of 0.005 keeps half a percent of entries, not none: the rate is never truncated to a whole percent."""
        monkeypatch.setattr("cachekit.logging.SAMPLING_RATE", 0.005)
        monkeypatch.setattr("cachekit.logging.random.random", lambda: draw)

        assert StructuredLogger("test.rate")._should_sample() is sampled

    def test_constructing_a_logger_starts_no_thread(self, monkeypatch):
        started: list[str] = []
        monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))

        StructuredLogger("test.threadless").error("boom")

        assert started == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
class TestEntriesAfterFork:
    """A forked child writes what it logs, with its own PID, and starts no thread doing so."""

    def test_os_fork_child_writes_its_error_with_its_own_pid(self, written):
        structured = get_structured_logger("test.os_fork")  # built pre-fork, as a module-level _logger is
        r, w = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                os.close(r)
                written.clear()  # the child's copy
                structured.error("child-error")
                outcome: object = [(e["message"], e["pid"]) for e in written]
            except BaseException as e:
                outcome = repr(e)
            report(w, outcome)
        os.close(w)

        assert child_outcome(child, r) == [("child-error", child)]

    def test_child_of_a_c_fork_writes_nothing_and_starts_no_thread(self):
        """A real fork from C: no at-fork hook runs, so logging's locks may be held by threads the child lacks.

        The child's calls return without taking them: they write nothing and request no thread, and its metrics still count.
        """
        run = run_fresh(_c_fork_child_outcome)

        assert run.returncode == 0, run.stderr
        assert ast.literal_eval(run.stdout) == {"requested": [], "written": [], "hits_counted": 1.0}, (
            run.stderr
        )  # a hung child's stack


class TestFactoryFunction:
    """Test factory function."""

    def test_get_structured_logger(self):
        """Test get_structured_logger factory returns one cached instance per name."""
        logger1 = get_structured_logger("test1")
        assert isinstance(logger1, StructuredLogger)

        logger1_again = get_structured_logger("test1")
        assert logger1_again is logger1
