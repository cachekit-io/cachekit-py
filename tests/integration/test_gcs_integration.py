"""Integration tests for GCSBackend against a Google Cloud Storage emulator.

Requires a running fake-gcs-server. Start one with:
    bash .github/scripts/start-fake-gcs.sh

Set STORAGE_EMULATOR_HOST to override the default http://localhost:4443. The tests only ever talk
to that host, never to Google Cloud Storage. They are skipped when it is unreachable. CI push runs
start the emulator and fail the job if any test here skips.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.file import FileBackend, FileBackendConfig

EMULATOR_HOST = os.environ.get("STORAGE_EMULATOR_HOST", "http://localhost:4443")


def _emulator_reachable() -> bool:
    """Whether the emulator answers, and google-cloud-storage is installed."""
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        return False
    if not EMULATOR_HOST.startswith(("http://", "https://")):
        return False
    try:
        # The scheme is checked above, and the host is this test run's own emulator.
        with urllib.request.urlopen(f"{EMULATOR_HOST}/storage/v1/b", timeout=2) as response:  # noqa: S310
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _emulator_reachable(), reason="GCS emulator not reachable, or the gcs extra is not installed"),
]


# Override autouse Redis fixture — GCS tests don't need Redis
@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override global Redis fixture — GCSBackend doesn't need Redis."""
    pass


@pytest.fixture(autouse=True)
def _emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every client built in a test talks to the emulator, with anonymous credentials."""
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", EMULATOR_HOST)


@pytest.fixture
def admin(_emulator_env: None) -> Any:
    """A plain storage client, to look at the bucket independently of the backend."""
    from google.cloud import storage

    return storage.Client()


@pytest.fixture
def bucket(admin: Any) -> Iterator[Any]:
    """A bucket no other test, xdist worker or concurrent run uses."""
    bucket = admin.create_bucket(f"cachekit-it-{uuid.uuid4().hex[:16]}")
    yield bucket
    bucket.delete(force=True)


@pytest.fixture
def backend(bucket: Any) -> Any:
    from cachekit.backends.gcs import GCSBackend, GCSBackendConfig

    return GCSBackend(GCSBackendConfig(bucket=bucket.name, timeout=5.0))


def _names(bucket: Any) -> list[str]:
    return sorted(blob.name for blob in bucket.list_blobs())


class TestRoundTrip:
    def test_set_get_exists_delete(self, backend: Any) -> None:
        assert backend.get("k") is None
        assert backend.exists("k") is False

        backend.set("k", b"value", ttl=60)

        assert backend.get("k") == b"value"
        assert backend.exists("k") is True
        assert backend.delete("k") is True
        assert backend.delete("k") is False
        assert backend.get("k") is None

    def test_set_replaces(self, backend: Any, bucket: Any) -> None:
        backend.set("k", b"one", ttl=60)
        backend.set("k", b"two")

        assert backend.get("k") == b"two"
        assert len(_names(bucket)) == 1

    @pytest.mark.parametrize("value", [b"", b"\x00\xff" * 512, os.urandom(9 * 1024 * 1024)], ids=["empty", "binary", "9MiB"])
    def test_values_survive(self, backend: Any, value: bytes) -> None:
        """9 MiB is past the client library's single-request limit, so that one is a resumable upload."""
        backend.set("k", value, ttl=60)
        assert backend.get("k") == value

    def test_concurrent_threads_share_a_backend(self, backend: Any) -> None:
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                for j in range(10):
                    key = f"thread-{i}-{j}"
                    backend.set(key, key.encode(), ttl=60)
                    assert backend.get(key) == key.encode()
            except BaseException as exc:  # noqa: BLE001 (reported below, on the main thread)
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []


class TestObjectFormat:
    @pytest.mark.parametrize("ttl", [None, 3600])
    def test_object_is_the_file_backends_file(self, backend: Any, bucket: Any, tmp_path: Path, ttl: int | None) -> None:
        """Same key, value and expiry: the stored object and the file backend's file are the same bytes, same name."""
        import time_machine

        key = "ns:app:func:mod.fn:args:" + "ab" * 32 + ":1s"
        with time_machine.travel(time.time(), tick=False):
            FileBackend(FileBackendConfig(cache_dir=tmp_path)).set(key, b"payload", ttl=ttl)
            backend.set(key, b"payload", ttl=ttl)

        (written,) = [p for p in tmp_path.iterdir() if p.is_file()]
        assert _names(bucket) == [written.name]
        assert bucket.blob(written.name).download_as_bytes() == written.read_bytes()

    def test_a_file_backend_entry_uploaded_as_is_is_readable(self, backend: Any, bucket: Any, tmp_path: Path) -> None:
        FileBackend(FileBackendConfig(cache_dir=tmp_path)).set("k", b"from a file", ttl=60)
        (written,) = [p for p in tmp_path.iterdir() if p.is_file()]
        bucket.blob(written.name).upload_from_filename(str(written))

        assert backend.get("k") == b"from a file"

    def test_prefix_is_part_of_the_object_name_only(self, bucket: Any) -> None:
        from cachekit.backends.gcs import GCSBackend, GCSBackendConfig

        plain = GCSBackend(GCSBackendConfig(bucket=bucket.name))
        prefixed = GCSBackend(GCSBackendConfig(bucket=bucket.name, prefix="cachekit/"))
        plain.set("k", b"plain")
        prefixed.set("k", b"prefixed")

        plain_name, prefixed_name = _names(bucket)
        assert prefixed_name == f"cachekit/{plain_name}"
        assert (plain.get("k"), prefixed.get("k")) == (b"plain", b"prefixed")


class TestExpiry:
    def test_expired_entry_is_a_miss_and_get_deletes_it(self, backend: Any, bucket: Any) -> None:
        backend.set("k", b"v", ttl=1)
        assert backend.exists("k") is True
        assert len(_names(bucket)) == 1

        time.sleep(1.1)  # the expiry is a whole second: the entry is dead once the clock reaches it

        assert backend.get("k") is None
        assert _names(bucket) == []

    def test_expired_entry_is_a_miss_on_exists(self, backend: Any) -> None:
        backend.set("k", b"v", ttl=1)
        time.sleep(1.1)
        assert backend.exists("k") is False

    def test_cleanup_spares_an_entry_stored_since_the_read(self, backend: Any) -> None:
        """A reader that saw a dead entry deletes that generation only, not a writer's newer one."""
        backend.set("k", b"dead")
        seen = backend._blob("k")
        seen.reload()  # the generation a read of the dead entry would have seen
        backend.set("k", b"fresh")

        backend._discard(seen)

        assert backend.get("k") == b"fresh"

    @pytest.mark.parametrize("body", [b"", b"CK", b"not a cachekit entry at all"], ids=["empty", "truncated", "bad-magic"])
    @pytest.mark.parametrize("read", ["get", "exists"])
    def test_corrupt_object_is_a_miss_and_is_deleted(self, backend: Any, bucket: Any, body: bytes, read: str) -> None:
        backend.set("k", b"v")
        (name,) = _names(bucket)
        bucket.blob(name).upload_from_string(body)

        assert not getattr(backend, read)("k")
        assert _names(bucket) == []

    @pytest.mark.parametrize("read", ["get", "exists"])
    def test_unknown_flag_is_a_miss_left_untouched(self, backend: Any, bucket: Any, read: str) -> None:
        backend.set("k", b"v")
        (name,) = _names(bucket)
        flagged = b"CK\x01\x00\x00\x01" + b"\x00" * 8 + b"transformed"
        bucket.blob(name).upload_from_string(flagged)

        assert not getattr(backend, read)("k")
        assert bucket.blob(name).download_as_bytes() == flagged


class TestHealthCheck:
    def test_healthy(self, backend: Any) -> None:
        healthy, details = backend.health_check()

        assert healthy is True
        assert details["backend_type"] == "gcs"
        assert details["latency_ms"] >= 0

    def test_missing_bucket_is_unhealthy(self, _emulator_env: None) -> None:
        from cachekit.backends.gcs import GCSBackend, GCSBackendConfig

        healthy, details = GCSBackend(GCSBackendConfig(bucket=f"cachekit-absent-{uuid.uuid4().hex[:16]}")).health_check()

        assert healthy is False
        assert "error" in details

    def test_set_to_a_missing_bucket_raises(self, _emulator_env: None) -> None:
        from cachekit.backends.gcs import GCSBackend, GCSBackendConfig

        with pytest.raises(BackendError) as exc_info:
            GCSBackend(GCSBackendConfig(bucket=f"cachekit-absent-{uuid.uuid4().hex[:16]}")).set("k", b"v")
        assert exc_info.value.error_type is BackendErrorType.PERMANENT


class TestDecoratorIntegration:
    def test_cache_with_explicit_backend(self, backend: Any, bucket: Any) -> None:
        from cachekit import cache

        calls = 0

        @cache(ttl=60, namespace="gcs_it", backend=backend, l1_enabled=False)
        def add(a: int, b: int) -> int:
            nonlocal calls
            calls += 1
            return a + b

        assert (add(2, 3), add(2, 3), calls) == (5, 5, 1)
        assert add(10, 20) == 30
        assert calls == 2
        assert len(_names(bucket)) == 2

    def test_env_selection_builds_a_working_backend(self, bucket: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from cachekit.backends.gcs import GCSBackend
        from cachekit.backends.provider import DefaultBackendProvider
        from tests.fixtures.zero_config import clear_cachekit_env

        clear_cachekit_env(monkeypatch)
        monkeypatch.setenv("CACHEKIT_GCS_BUCKET", bucket.name)
        monkeypatch.setenv("CACHEKIT_GCS_PREFIX", "env/")

        selected = DefaultBackendProvider().get_backend()

        assert isinstance(selected, GCSBackend)
        selected.set("k", b"v", ttl=60)
        assert selected.get("k") == b"v"
        assert _names(bucket)[0].startswith("env/")
