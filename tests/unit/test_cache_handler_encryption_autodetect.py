"""Tests for CacheSerializationHandler encryption auto-detection.

When CACHEKIT_MASTER_KEY is set and encryption is not explicitly configured
(encryption=None), the handler still auto-enables encryption with single_tenant_mode=True —
DEPRECATED (protocol intent-presets.md § Encryption Activation): this release warns once per
process, the next minor release raises at construction instead.

Encryption is tri-state (issue #128): None=unset, True=force-on, False=hard opt-out. An
explicit False must survive a present CACHEKIT_MASTER_KEY.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from typing import Any

import pytest

import cachekit.cache_handler as cache_handler_mod
from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.singleton import reset_settings
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.wrapper import SerializationWrapper

_FAKE_KEY = "ab" * 32  # pragma: allowlist secret
_DEPLOYMENT_UUID = "00000000-0000-0000-0000-000000000001"


def _envelope_is_encrypted(handler: CacheSerializationHandler, data: object, cache_key: str) -> bool:
    """Serialize and inspect the on-the-wire envelope's encrypted metadata flag."""
    blob = handler.serialize_data(data, cache_key=cache_key)
    _serialized, metadata_dict, _name = SerializationWrapper.unwrap(blob)
    return SerializationMetadata.from_dict(metadata_dict).encrypted


@pytest.mark.unit
class TestEncryptionAutoDetect:
    """CacheSerializationHandler auto-detects CACHEKIT_MASTER_KEY."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        reset_settings()

    def test_auto_detect_enables_encryption(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler enables encryption when CACHEKIT_MASTER_KEY is set."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is True
        assert handler.master_key == _FAKE_KEY
        assert handler.single_tenant_mode is True

    def test_auto_detect_no_op_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler stays plaintext when CACHEKIT_MASTER_KEY is not set."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is False
        assert handler.master_key is None

    def test_auto_detect_no_op_when_explicitly_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit encryption=True is not overwritten by env var."""
        env_key = "ab" * 32  # pragma: allowlist secret
        explicit_key = "cc" * 32  # pragma: allowlist secret
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", env_key)
        reset_settings()

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            master_key=explicit_key,
            single_tenant_mode=True,
        )

        assert handler.master_key == explicit_key

    def test_auto_detect_no_op_when_tenant_extractor_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If tenant_extractor is passed, auto-detect is skipped (user expressing intent)."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        def extractor(*a, **kw):
            return "tenant-1"

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=False,
            tenant_extractor=extractor,
        )

        # Explicit encryption=False is a hard opt-out — auto-detect never runs
        assert handler.encryption is False


@pytest.mark.unit
class TestEncryptionTriState:
    """Tri-state encryption: None=auto, True=force-on, False=explicit opt-out (issue #128)."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        reset_settings()

    def test_default_param_is_none_auto_detects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """encryption defaults to None (unset) and auto-detects from the env key."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is True
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:auto") is True

    def test_explicit_false_opts_out_despite_master_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REGRESSION (issue #128): encryption=False must NOT auto-encrypt when a key is set.

        Before the tri-state fix, the auto-detect guard couldn't distinguish "unset" from
        "explicitly False" (both were the bool False), so a deliberate opt-out was silently
        promoted to encryption=True by fleet-wide CACHEKIT_MASTER_KEY.
        """
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default", encryption=False)

        assert handler.encryption is False
        assert handler.master_key is None
        # The on-the-wire envelope must be plaintext, not ciphertext.
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:optout") is False

    def test_explicit_true_forces_encryption_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """encryption=True forces encryption on (single-tenant) even via the env key."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            single_tenant_mode=True,
        )

        assert handler.encryption is True
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:forced") is True

    def test_decorator_explicit_false_yields_plaintext_bare_encrypts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end via @cache: bare encrypts, encryption=False opts out, encryption=True forces on."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        from cachekit.config.decorator import DecoratorConfig
        from cachekit.config.nested import EncryptionConfig

        # Bare @cache leaves encryption unset (None) -> auto-detect path stays available.
        # Construction runs full validation via __post_init__; no raise == valid.
        bare = DecoratorConfig(backend=None)
        assert bare.encryption.enabled is None

        # @cache(encryption=False) maps to an explicit opt-out that survives the env key.
        # Explicit False never requires a master key (validates on construction).
        opted_out = DecoratorConfig(backend=None, encryption=EncryptionConfig(enabled=False))
        assert opted_out.encryption.enabled is False

        # @cache(encryption=True) validates against the env-resolved key (force-on).
        # No inline key needed: __post_init__ resolves CACHEKIT_MASTER_KEY from env.
        forced = DecoratorConfig(
            backend=None,
            encryption=EncryptionConfig(enabled=True, single_tenant_mode=True),
        )
        assert forced.encryption.enabled is True


@pytest.mark.unit
class TestDecoratorEncryptionFlattening:
    """`@cache(...)` folds flat encryption kwargs into a nested EncryptionConfig (issue #128).

    Covers the bare-decorator mapping block in ``cachekit.decorators.intent.cache`` that turns
    flat ``encryption`` / ``master_key`` / ``tenant_extractor`` / ``single_tenant_mode`` /
    ``deployment_uuid`` kwargs into ``DecoratorConfig.encryption``. This is the path that lets a
    deliberate per-function ``encryption=False`` survive all the way to config resolution.

    The wrapper factory is patched out so we assert on the resolved DecoratorConfig directly,
    without constructing a real (Rust-backed) cache wrapper or touching a backend.
    """

    @pytest.fixture
    def captured_config(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import cachekit.decorators.intent as intent_mod

        captured: dict[str, Any] = {}

        def _fake_wrapper(func: Any, *, config: Any, _l1_only_mode: bool = False, **_kwargs: Any) -> Any:
            captured["config"] = config
            return func

        monkeypatch.setattr(intent_mod, "create_cache_wrapper", _fake_wrapper)
        return captured

    def test_flat_encryption_false_maps_to_opt_out(self, captured_config: dict[str, Any]) -> None:
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=False, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is False

    def test_flat_encryption_true_maps_to_force_on(self, captured_config: dict[str, Any]) -> None:
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=True, master_key=_FAKE_KEY, single_tenant_mode=True, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is True
        assert enc.single_tenant_mode is True

    def test_flat_key_params_fold_in_without_encryption_flag(self, captured_config: dict[str, Any]) -> None:
        """master_key / single_tenant_mode / deployment_uuid fold in even when `encryption`
        is omitted — `enabled` stays None (auto), exercising the per-key loop branch."""
        from cachekit.config.nested import EncryptionConfig

        @cache(master_key=_FAKE_KEY, single_tenant_mode=True, deployment_uuid=_DEPLOYMENT_UUID, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is None
        assert enc.master_key == _FAKE_KEY
        assert enc.single_tenant_mode is True
        assert enc.deployment_uuid == _DEPLOYMENT_UUID

    def test_prebuilt_encryption_config_passes_through_unwrapped(self, captured_config: dict[str, Any]) -> None:
        """An already-constructed EncryptionConfig is NOT re-wrapped (would nest a config in
        `.enabled`); the passthrough guard skips the mapping block."""
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=EncryptionConfig(enabled=False), backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        # If the guard failed, enabled would be an EncryptionConfig, not the bool False.
        assert enc.enabled is False


@pytest.mark.unit
class TestAutoActivationDeprecationWarning:
    """Release-N migration gate: presence-activation warns ONCE per process via logger.warning,
    naming the explicit spellings (`@cache.secure(...)`, `encryption=True`, `encryption=False`)
    so the fix is copy-pasteable from the log line.
    """

    @pytest.fixture(autouse=True)
    def _fresh_process(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(cache_handler_mod, "_AUTO_ACTIVATION_WARNED_PIDS", {})
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()
        yield
        reset_settings()

    @staticmethod
    def _activation_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno == logging.WARNING and "auto-enabled" in r.message]

    def test_warns_once_per_process_and_names_the_explicit_spellings(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="cachekit.cache_handler"):
            first = CacheSerializationHandler(serializer_name="default")
            second = CacheSerializationHandler(serializer_name="default")

        # Release N still activates — only the warning is new.
        assert first.encryption is True and second.encryption is True
        records = self._activation_records(caplog)
        assert len(records) == 1, [r.message for r in records]
        assert "@cache.secure(" in records[0].message
        assert "encryption=True" in records[0].message
        assert "encryption=False" in records[0].message
        # An L1-only cache warns too but holds raw objects: the line must not claim every cache encrypts.
        assert "L1-only cache (backend=None)" in records[0].message

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"encryption": False}, id="explicit-false"),
            pytest.param({"encryption": True, "single_tenant_mode": True}, id="explicit-true"),
        ],
    )
    def test_explicit_intent_does_not_warn(self, caplog: pytest.LogCaptureFixture, kwargs: dict[str, Any]) -> None:
        with caplog.at_level(logging.WARNING, logger="cachekit.cache_handler"):
            CacheSerializationHandler(serializer_name="default", **kwargs)
        assert self._activation_records(caplog) == []

    def test_no_key_does_not_warn(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY")
        reset_settings()
        with caplog.at_level(logging.WARNING, logger="cachekit.cache_handler"):
            handler = CacheSerializationHandler(serializer_name="default")
        assert handler.encryption is False
        assert self._activation_records(caplog) == []

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_forked_child_warns_for_itself(self, caplog: pytest.LogCaptureFixture) -> None:
        """A forked worker is a new process: it must not inherit the parent's already-fired warning."""
        with caplog.at_level(logging.WARNING, logger="cachekit.cache_handler"):
            CacheSerializationHandler(serializer_name="default")
            assert len(self._activation_records(caplog)) == 1

            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q: Any) -> None:
                caplog.clear()  # the child's copy still holds the parent's record
                CacheSerializationHandler(serializer_name="default")
                q.put(len(self._activation_records(caplog)))

            process = ctx.Process(target=child, args=(queue,))
            process.start()
            try:
                child_warnings = queue.get(timeout=30)
            finally:
                process.join(timeout=30)
                if process.is_alive():  # a hung child would otherwise block pytest's exit forever
                    process.kill()
                    process.join()

        assert child_warnings == 1

    def test_racing_constructions_warn_once(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Threads that reach the once-per-process decision together must still log it once.

        The PID stand-in parks each thread at a barrier each time the guard compares or hashes it, so
        every thread has checked before any records the warning: the interleaving under which a
        check-then-set, compare or membership, logs once per thread (free-threaded builds reach it
        without help).
        """
        threads = 8
        barrier = threading.Barrier(threads, timeout=5)  # a lock-serialised guard breaks it, then carries on

        class _ParkingPid(int):
            def _park(self) -> None:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass

            def __eq__(self, other: object) -> bool:
                self._park()
                return super().__eq__(other)

            def __hash__(self) -> int:
                self._park()
                return super().__hash__()

        pid = _ParkingPid(os.getpid())
        monkeypatch.setattr(os, "getpid", lambda: pid)
        built: list[CacheSerializationHandler] = []
        workers = [
            threading.Thread(target=lambda: built.append(CacheSerializationHandler(serializer_name="default")), daemon=True)
            for _ in range(threads)
        ]
        with caplog.at_level(logging.WARNING, logger="cachekit.cache_handler"):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)

        assert not any(worker.is_alive() for worker in workers)
        assert len(built) == threads  # a raising constructor would otherwise vanish into a thread warning
        assert len(self._activation_records(caplog)) == 1
