"""A cache decorator refuses an encrypting serializer when it is applied, in every mode.

EncryptionWrapper binds each ciphertext to its cache key, and a decorator never gives a key to a
serializer it was handed, so ``serializer=`` with an EncryptionWrapper instance or the ``"encrypted"``
name could never store an entry. Depending on the preset and on ``CACHEKIT_MASTER_KEY``, the decorator
either applied cleanly and ran the function on every call, or failed with an error that did not name
the supported spelling. Encryption on a decorator is ``@cache.secure(master_key=..., serializer=...)``,
which builds the wrapper itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cachekit import DecoratorConfig, cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.config import ConfigurationError
from cachekit.config.nested import EncryptionConfig
from cachekit.config.singleton import reset_settings
from cachekit.serializers import EncryptionWrapper, StandardSerializer

pytestmark = pytest.mark.unit

_KEY_HEX = "ab" * 32  # pragma: allowlist secret
_API_KEY = "ck_test_encryptingSerializer0123456789"  # pragma: allowlist secret
# Text only the new refusal carries. The no-intent error also names @cache.secure(...), so matching on
# that would pass the env-key half of the matrix without the refusal.
_MATCH = "cannot be a cache decorator's serializer"


def _wrapper() -> EncryptionWrapper:
    return EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX))


class _SubclassedWrapper(EncryptionWrapper):
    __slots__ = ()


# Each spelling takes the configured backend; @cache.io builds its own.
_SPELLINGS: dict[str, Callable[[FileBackend], Callable[[Callable[..., Any]], Any]]] = {
    "bare-instance": lambda b: cache(serializer=_wrapper(), backend=b),
    "bare-name": lambda b: cache(serializer="encrypted", backend=b),
    "bare-instance-opt-out": lambda b: cache(serializer=_wrapper(), encryption=False, backend=b),
    "bare-name-opt-out": lambda b: cache(serializer="encrypted", encryption=False, backend=b),
    "bare-config-opt-out": lambda b: cache(serializer=_wrapper(), encryption=EncryptionConfig(enabled=False), backend=b),
    "bare-force-on": lambda b: cache(
        serializer=_wrapper(), encryption=True, single_tenant_mode=True, master_key=_KEY_HEX, backend=b
    ),
    "bare-subclass": lambda b: cache(serializer=_SubclassedWrapper(master_key=bytes.fromhex(_KEY_HEX)), backend=b),
    "secure-instance": lambda b: cache.secure(master_key=_KEY_HEX, serializer=_wrapper(), backend=b),
    "secure-name": lambda b: cache.secure(master_key=_KEY_HEX, serializer="encrypted", backend=b),
    "production-instance": lambda b: cache.production(serializer=_wrapper(), backend=b),
    "production-instance-opt-out": lambda b: cache.production(serializer=_wrapper(), encryption=False, backend=b),
    "minimal-instance": lambda b: cache.minimal(serializer=_wrapper(), backend=b),
    "dev-instance": lambda b: cache.dev(serializer=_wrapper(), backend=b),
    "test-instance": lambda b: cache.test(serializer=_wrapper(), backend=b),
    "io-instance": lambda _b: cache.io(api_key=_API_KEY, serializer=_wrapper()),
    "roro-instance": lambda b: cache(config=DecoratorConfig(serializer=_wrapper(), backend=b)),
}


def _cached(x: int) -> dict[str, int]:
    return {"x": x}


@pytest.fixture
def backend(tmp_path: Path) -> FileBackend:
    return FileBackend(FileBackendConfig(cache_dir=tmp_path))


@pytest.fixture(params=[None, _KEY_HEX], ids=["env-key-unset", "env-key-set"])
def env_key(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.param is None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
    else:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", request.param)
    reset_settings()


@pytest.mark.usefixtures("env_key")
class TestEncryptingSerializerRefused:
    @pytest.mark.parametrize("spelling", _SPELLINGS)
    def test_raises_at_decoration_naming_cache_secure(self, spelling: str, backend: FileBackend) -> None:
        with pytest.raises(ConfigurationError, match=_MATCH) as error:
            _SPELLINGS[spelling](backend)(_cached)

        assert "@cache.secure(master_key=..., serializer=...)" in str(error.value)

    def test_inner_serializer_on_cache_secure_still_caches(self, backend: FileBackend) -> None:
        """The spelling the refusal names decorates and serves the second call from the backend.

        L1 is off so the hit has to come through the backend, not from the process-wide L1 entry
        the other parametrization of this test wrote under the same key.
        """
        calls = 0

        @cache.secure(master_key=_KEY_HEX, serializer=StandardSerializer(), backend=backend, l1_enabled=False)
        def fetch(x: int) -> dict[str, int]:
            nonlocal calls
            calls += 1
            return {"x": x}

        assert fetch(1) == fetch(1) == {"x": 1}
        assert calls == 1
