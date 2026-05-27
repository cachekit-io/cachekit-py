"""Encrypted-payload E2E smoke for ``@cache.io`` against a live CacheKit SaaS backend.

Encryption on the SaaS path is switched on by setting ``CACHEKIT_MASTER_KEY``:
``@cache.io`` then encrypts client-side (AES-256-GCM) before any byte leaves the
process, and the SaaS stores opaque ciphertext. (The env key enables encryption
fleet-wide via settings — no decorator change needed.) Only a holder of the
master key can recover the plaintext; the SaaS never sees the key.

``@cache.io`` builds its own CachekitIO backend and ignores an injected one, so
the ciphertext is captured for the zero-knowledge / tamper checks via a
class-level monkeypatch of ``CachekitIOBackend.set``.

Two constraints encoded here:
    1. The SaaS enforces a strict cache-key format — ``func`` must match
       ``[a-zA-Z0-9_.]{1,200}``. Functions nested in test methods carry a
       ``<locals>`` qualname the SaaS rejects (HTTP 400), so the cached target
       is the *module-level* ``_return_registered`` below.
    2. The decorator fails *open* on a decrypt failure — tampered ciphertext is
       treated as a miss and silently recomputed, not raised. So the tamper test
       swaps the registry value between write and read: a read returning the
       *new* value proves the stored bytes could not be decrypted and were
       recomputed, never served forged.

    (Wrong-key isolation is intentionally NOT tested here: an in-process cache
    keyed by cache-key serves the value regardless of master key, so a
    decorator-level rotation test is unreliable. That guarantee lives in the
    encryption unit tests, e.g. tests/unit/test_security_properties.py.)

Env-parameterized so it targets any environment (validate on dev, gate on prod):
    CACHEKIT_API_KEY            required — ck_sdk_... ; the whole module skips if absent
    CACHEKIT_API_URL            default https://api.cachekit.io
    CACHEKIT_ALLOW_CUSTOM_HOST  set "true" for non-allowlisted hosts (e.g. api.dev.cachekit.io)
    CACHEKIT_MASTER_KEY         set per-test by the io_env fixture to enable encryption
    CACHEKIT_NAMESPACE          default secure_e2e
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator

import pytest

from cachekit import cache
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.config.singleton import reset_settings
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper

API_URL = os.getenv("CACHEKIT_API_URL", "https://api.cachekit.io")
API_KEY = os.getenv("CACHEKIT_API_KEY")

pytestmark = [
    pytest.mark.sdk_e2e,
    pytest.mark.security,
    pytest.mark.skipif(
        not API_KEY,
        reason="set CACHEKIT_API_KEY (ck_sdk_...) to run the live SaaS encrypted-payload smoke",
    ),
]

# Registry feeding the module-level cached target. Keyed by a per-test tag so
# tests never collide (the tag is also a cache-key argument).
_RESULT: dict[str, object] = {}

_STD = StandardSerializer()


def _return_registered(tag: str) -> object:
    """Module-level cache target — its qualname is SaaS-key-compliant (no `<locals>`)."""
    return _RESULT[tag]


def _plaintext_recoverable(raw: bytes, expected: object) -> bool:
    """True if the *unencrypted* standard serializer reads `raw` back to `expected`.

    Unwraps the SerializationWrapper JSON envelope first so the check
    operates on the actual payload bytes, not the envelope.  Without
    this, the envelope itself would defeat the recovery check even for
    plaintext data — a false negative.
    """
    try:
        inner_bytes, _meta, _name = SerializationWrapper.unwrap(raw)
        return _STD.deserialize(inner_bytes) == expected
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override the root-conftest autouse Redis-isolation fixture (these tests use the SaaS, not Redis)."""
    yield


@pytest.fixture
def master_key() -> str:
    """Fresh 256-bit (64 hex char) master key per test — client-side only."""
    return secrets.token_hex(32)


@pytest.fixture
def namespace() -> str:
    base = os.getenv("CACHEKIT_NAMESPACE", "secure_e2e")
    return f"{base}_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def io_env(master_key: str) -> Iterator[None]:
    """Configure the env so ``@cache.io`` targets the SaaS *and* encrypts.

    Sets the API key/URL, the custom-host override for non-allowlisted hosts
    (dev), and CACHEKIT_MASTER_KEY (which turns encryption on), then resets the
    cached settings singleton so the values take effect.
    """
    keys = ("CACHEKIT_API_KEY", "CACHEKIT_API_URL", "CACHEKIT_ALLOW_CUSTOM_HOST", "CACHEKIT_MASTER_KEY")
    previous = {k: os.environ.get(k) for k in keys}
    os.environ["CACHEKIT_API_KEY"] = API_KEY or ""
    os.environ["CACHEKIT_API_URL"] = API_URL
    os.environ["CACHEKIT_MASTER_KEY"] = master_key
    if API_URL not in ("https://api.cachekit.io", "https://api.staging.cachekit.io"):
        os.environ["CACHEKIT_ALLOW_CUSTOM_HOST"] = "true"
    reset_settings()
    try:
        yield
    finally:
        for key, val in previous.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        reset_settings()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Record the last (key, value) the SDK writes to the SaaS.

    ``@cache.io`` builds its own backend, so we patch the class method rather
    than inject — capturing the real SaaS-compliant key and the exact bytes sent.
    """
    holder: dict[str, object] = {"key": None, "value": None}
    original = CachekitIOBackend.set

    def recording_set(self: CachekitIOBackend, key: str, value: bytes, ttl: int | None = None) -> None:
        holder["key"] = key
        holder["value"] = value
        original(self, key, value, ttl)

    monkeypatch.setattr(CachekitIOBackend, "set", recording_set)
    return holder


def _new_tag() -> str:
    return uuid.uuid4().hex


@pytest.mark.usefixtures("io_env")
class TestEncryptedRoundTrip:
    """Encrypt -> store in SaaS -> decrypt == original, via @cache.io + master key."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"pii": "ssn-123-45-6789", "name": "Ada"}, id="dict-pii"),
            pytest.param([1, 2, 3, {"x": True, "y": None}], id="list-mixed"),
            pytest.param(b"\x00\x01raw-bytes\xff\xfe", id="raw-bytes"),
            pytest.param({"nested": {"a": {"b": [1, {"c": None}]}}, "u": "lock-\U0001f512"}, id="nested-unicode"),
        ],
    )
    def test_roundtrip_survives_saas(self, payload: object, namespace: str, captured: dict[str, object]) -> None:
        tag = _new_tag()
        _RESULT[tag] = payload
        fetch = cache.io(namespace=namespace, ttl=300)(_return_registered)

        assert fetch(tag) == payload  # encrypt + store in SaaS

        raw = captured["value"]
        assert isinstance(raw, (bytes, bytearray)), "ciphertext never reached the SaaS"
        assert not _plaintext_recoverable(raw, payload), "payload stored unencrypted"

        fetch.cache_clear()  # drop L1 — force the next read through the SaaS
        assert fetch(tag) == payload  # fetched from SaaS + decrypted


@pytest.mark.usefixtures("io_env")
class TestZeroKnowledge:
    """The product claim: the bytes leaving the client are opaque ciphertext."""

    def test_stored_bytes_are_ciphertext(self, namespace: str, captured: dict[str, object]) -> None:
        sentinel = f"TOPSECRET-{uuid.uuid4().hex}"
        tag = _new_tag()
        _RESULT[tag] = {"secret": sentinel}
        fetch = cache.io(namespace=namespace, ttl=300)(_return_registered)

        assert fetch(tag)["secret"] == sentinel  # client sees plaintext

        raw = captured["value"]
        assert isinstance(raw, (bytes, bytearray)), "no ciphertext captured"
        assert sentinel.encode() not in raw, "plaintext value leaked into stored bytes"
        assert not _plaintext_recoverable(raw, {"secret": sentinel}), "stored bytes are not encrypted"


@pytest.mark.usefixtures("io_env")
class TestTamperDetection:
    """AES-256-GCM's auth tag must reject mutated ciphertext — forged data is never served."""

    def test_mutated_ciphertext_is_rejected(self, namespace: str, captured: dict[str, object]) -> None:
        tag = _new_tag()
        original = {"secret": "genuine"}  # pragma: allowlist secret
        _RESULT[tag] = original
        fn = cache.io(namespace=namespace, ttl=300)(_return_registered)

        fn(tag)  # store genuine ciphertext
        key = captured["key"]
        ciphertext = captured["value"]
        assert isinstance(key, str) and isinstance(ciphertext, (bytes, bytearray))

        mutated = bytearray(ciphertext)
        mutated[-1] ^= 0x01  # flip one bit of the GCM tag region
        CachekitIOBackend().set(key, bytes(mutated))  # overwrite the SaaS entry at the SDK's own key

        fn.cache_clear()  # bust L1 so the next call must read the tampered bytes from the SaaS

        # Swap the registry to detect recompute vs. serving the tampered bytes.
        recomputed_marker = {"secret": "recomputed-after-tamper"}  # pragma: allowlist secret
        _RESULT[tag] = recomputed_marker

        result = fn(tag)
        assert result == recomputed_marker, "GCM tamper rejection failed: SDK served forged/garbage data"
