"""Unit tests for GCSBackend.

The backend runs on the real google-cloud-storage client over a scripted HTTP transport, so each
test sees the requests the backend sends: how many, which method, which headers. Nothing leaves
the process. tests/integration/test_gcs_integration.py covers a live server.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import time_machine

pytest.importorskip("google.cloud.storage", reason="needs the gcs extra")

import google_crc32c
import requests
import requests.adapters
import urllib3
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.cloud.storage.exceptions import DataCorruption

from cachekit.backends.base import BaseBackend, TTLInspectableBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.backends.gcs import GCSBackend, GCSBackendConfig
from cachekit.backends.gcs.error_handler import classify_gcs_error

BUCKET = "cache-bucket"
FIXTURE_PATH = Path(__file__).parents[1] / "protocol" / "fixtures" / "file-backend.json"
VECTORS: list[dict[str, Any]] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["vectors"]


def _header(expiry: int = 0, reserved: int = 0, flags: int = 0) -> bytes:
    return b"CK\x01" + bytes([reserved]) + struct.pack(">H", flags) + struct.pack(">Q", expiry)


def _name(key: str) -> str:
    return hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()


class _Reply:
    """One scripted HTTP response, or the exception the transport raises instead."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        generation: int | None = None,
        error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.body = body
        self.error = error
        self.headers = dict(headers or {})
        if generation is not None:
            self.headers["X-Goog-Generation"] = str(generation)


class _Transport(requests.adapters.BaseAdapter):
    """Answers each request with the next scripted reply, and records the request."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[_Reply] = []
        self.requests: list[requests.PreparedRequest] = []
        self._build = requests.adapters.HTTPAdapter()  # for build_response only: it sends nothing

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.requests.append(request)
        assert self.replies, f"unscripted request: {request.method} {request.url}"
        reply = self.replies.pop(0)
        if reply.error is not None:
            raise reply.error
        headers = {"Content-Length": str(len(reply.body)), **reply.headers}
        raw = urllib3.HTTPResponse(body=io.BytesIO(reply.body), headers=headers, status=reply.status, preload_content=False)
        return self._build.build_response(request, raw)

    def close(self) -> None:
        pass

    def script(self, *replies: _Reply) -> None:
        self.replies.extend(replies)

    def sent(self) -> list[tuple[str, str]]:
        """(method, object name or bucket-relative path) of every request so far."""
        return [(r.method or "", unquote(urlsplit(r.url or "").path.split(f"/b/{BUCKET}", 1)[1])) for r in self.requests]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _Transport:
    """The transport every client built by GCSBackend talks to."""
    transport = _Transport()

    def build(project: str | None) -> storage.Client:
        credentials = AnonymousCredentials()
        session = AuthorizedSession(credentials)
        session.mount("https://", transport)
        return storage.Client(project=project or "test-project", credentials=credentials, _http=session)

    monkeypatch.setattr("cachekit.backends.gcs.backend._build_client", build)
    return transport


@pytest.fixture
def backend(transport: _Transport) -> GCSBackend:
    return GCSBackend(GCSBackendConfig(bucket=BUCKET))


def _if_generation_match(request: requests.PreparedRequest) -> list[str] | None:
    return parse_qs(urlsplit(request.url or "").query).get("ifGenerationMatch")


def _uploaded(request: requests.PreparedRequest) -> bytes:
    """The object bytes of a multipart upload request."""
    content_type = request.headers["content-type"]
    if isinstance(content_type, str):
        content_type = content_type.encode()
    boundary = content_type.split(b'boundary="', 1)[1].rstrip(b'"')
    body = request.body
    assert isinstance(body, bytes)
    media = body.split(b"--" + boundary)[2]  # preamble, metadata part, media part, epilogue
    return media.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")


@pytest.mark.unit
class TestProtocolCompliance:
    def test_is_a_base_backend(self, backend: GCSBackend) -> None:
        assert isinstance(backend, BaseBackend)

    def test_is_not_ttl_inspectable(self, backend: GCSBackend) -> None:
        """Objects are immutable: refreshing a TTL would rewrite the whole object."""
        assert not isinstance(backend, TTLInspectableBackend)

    def test_construction_sends_no_request(self, transport: _Transport) -> None:
        GCSBackend(GCSBackendConfig(bucket=BUCKET))
        assert transport.requests == []


@pytest.mark.unit
class TestGet:
    def test_hit_is_one_unranged_download(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(body=_header() + b"value"))

        assert backend.get("k") == b"value"

        assert transport.sent() == [("GET", f"/o/{_name('k')}")]
        assert "Range" not in transport.requests[0].headers

    def test_absent_object_is_a_miss_in_one_request(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=404))

        assert backend.get("k") is None
        assert len(transport.requests) == 1

    def test_empty_payload_round_trips(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(body=_header()))
        assert backend.get("k") == b""

    @time_machine.travel(1_000_000, tick=False)
    def test_entry_is_live_until_its_expiry_second(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(body=_header(expiry=1_000_001) + b"v"))
        assert backend.get("k") == b"v"

    @time_machine.travel(1_000_000, tick=False)
    def test_expired_entry_is_a_miss_and_is_deleted(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(body=_header(expiry=1_000_000) + b"v", generation=7), _Reply(status=204, body=b""))

        assert backend.get("k") is None

        assert transport.sent() == [("GET", f"/o/{_name('k')}"), ("DELETE", f"/o/{_name('k')}")]
        # Only the generation that was read: an entry stored in between survives.
        assert _if_generation_match(transport.requests[1]) == ["7"]

    @pytest.mark.parametrize(
        "body",
        [b"", b"CK\x01", _header()[:13], b"XX" + _header()[2:] + b"v", b"CK\x02" + _header()[3:] + b"v"],
        ids=["empty", "short", "one-byte-short", "bad-magic", "bad-version"],
    )
    def test_corrupt_entry_is_a_miss_and_is_deleted(self, backend: GCSBackend, transport: _Transport, body: bytes) -> None:
        transport.script(_Reply(body=body, generation=3), _Reply(status=204, body=b""))

        assert backend.get("k") is None
        assert [method for method, _ in transport.sent()] == ["GET", "DELETE"]
        assert _if_generation_match(transport.requests[1]) == ["3"]

    def test_dead_entry_of_unknown_generation_is_not_deleted(self, backend: GCSBackend, transport: _Transport) -> None:
        """With no generation to condition on, a delete could remove an entry stored since the read."""
        transport.script(_Reply(body=b"CK"))

        assert backend.get("k") is None
        assert [method for method, _ in transport.sent()] == ["GET"]

    @pytest.mark.parametrize("reserved,flags", [(1, 0), (0, 1), (0, 0x8000)])
    @time_machine.travel(1_000_000, tick=False)
    def test_unknown_transform_is_a_miss_left_untouched(
        self, backend: GCSBackend, transport: _Transport, reserved: int, flags: int
    ) -> None:
        """Even when expired: the flags are judged before the expiry."""
        transport.script(_Reply(body=_header(expiry=1, reserved=reserved, flags=flags) + b"v", generation=3))

        assert backend.get("k") is None
        assert [method for method, _ in transport.sent()] == ["GET"]

    @pytest.mark.parametrize("cleanup", [_Reply(status=403), _Reply(status=412), _Reply(error=requests.ConnectionError())])
    @time_machine.travel(1_000_000, tick=False)
    def test_failed_cleanup_is_still_a_miss(self, backend: GCSBackend, transport: _Transport, cleanup: _Reply) -> None:
        """A read-only identity, or an entry replaced since the read, cannot fail the read."""
        transport.script(_Reply(body=_header(expiry=5) + b"v", generation=3), cleanup)
        assert backend.get("k") is None


@pytest.mark.unit
class TestExists:
    def test_reads_only_the_header(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=206, body=_header()))

        assert backend.exists("k") is True

        assert transport.sent() == [("GET", f"/o/{_name('k')}")]
        assert transport.requests[0].headers["Range"] == "bytes=0-13"

    def test_header_is_not_checked_against_the_whole_objects_checksum(self, backend: GCSBackend, transport: _Transport) -> None:
        """A ranged response still carries the checksum of the whole object, which 14 bytes never match."""
        whole = base64.b64encode(google_crc32c.Checksum(_header() + b"a long payload").digest()).decode()
        transport.script(_Reply(status=206, body=_header(), headers={"X-Goog-Hash": f"crc32c={whole}"}))

        assert backend.exists("k") is True

    def test_absent_object(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=404))
        assert backend.exists("k") is False

    @time_machine.travel(1_000_000, tick=False)
    def test_expired_entry_is_a_miss(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=206, body=_header(expiry=999_999), generation=4), _Reply(status=204, body=b""))
        assert backend.exists("k") is False

    def test_empty_object_is_a_miss_and_is_deleted(self, backend: GCSBackend, transport: _Transport) -> None:
        """An empty object has no first byte, so the ranged read is refused with a 416.

        The 416 names no generation, so the object is read again, whole, before the conditional delete.
        """
        transport.script(_Reply(status=416), _Reply(body=b"", generation=5), _Reply(status=204, body=b""))

        assert backend.exists("k") is False
        assert [method for method, _ in transport.sent()] == ["GET", "GET", "DELETE"]
        assert "Range" not in transport.requests[1].headers
        assert _if_generation_match(transport.requests[2]) == ["5"]

    def test_entry_stored_over_an_empty_object_is_found(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=416), _Reply(body=_header() + b"v", generation=6))

        assert backend.exists("k") is True
        assert len(transport.requests) == 2

    def test_unknown_transform_is_a_miss_left_untouched(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=206, body=_header(flags=1)))

        assert backend.exists("k") is False
        assert len(transport.requests) == 1


@pytest.mark.unit
class TestSet:
    @time_machine.travel(1_000_000, tick=False)
    def test_is_one_upload_of_header_and_value(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply())

        backend.set("k", b"value", ttl=60)

        assert [r.method for r in transport.requests] == ["POST"]
        request = transport.requests[0]
        assert parse_qs(urlsplit(request.url or "").query)["uploadType"] == ["multipart"]
        assert _uploaded(request) == _header(expiry=1_000_060) + b"value"
        body = request.body
        assert isinstance(body, bytes)
        assert json.loads(body.split(b"\r\n\r\n", 2)[1].split(b"\r\n", 1)[0])["name"] == _name("k")

    @pytest.mark.parametrize("ttl", [None, 0])
    def test_no_ttl_writes_no_expiry(self, backend: GCSBackend, transport: _Transport, ttl: int | None) -> None:
        transport.script(_Reply())
        backend.set("k", b"v", ttl=ttl)
        assert _uploaded(transport.requests[0]) == _header(expiry=0) + b"v"

    @pytest.mark.parametrize("ttl", [-1, 10 * 365 * 24 * 60 * 60 + 1])
    def test_out_of_range_ttl_is_refused_before_any_request(self, backend: GCSBackend, transport: _Transport, ttl: int) -> None:
        with pytest.raises(BackendError) as exc_info:
            backend.set("k", b"v", ttl=ttl)

        assert exc_info.value.error_type is BackendErrorType.PERMANENT
        assert transport.requests == []

    @pytest.mark.parametrize("ttl", [None, 0, 1, 3600])
    @pytest.mark.parametrize("value", [b"", b"value", bytes(range(256)) * 4])
    @time_machine.travel(1_700_000_000, tick=False)
    def test_object_is_byte_identical_to_the_file_backend(
        self, backend: GCSBackend, transport: _Transport, tmp_path: Path, value: bytes, ttl: int | None
    ) -> None:
        key = "ns:app:func:mod.fn:args:" + "ab" * 32 + ":1s"
        file_backend = FileBackend(FileBackendConfig(cache_dir=tmp_path))
        file_backend.set(key, value, ttl=ttl)
        (written,) = [p for p in tmp_path.iterdir() if p.is_file()]
        transport.script(_Reply())

        backend.set(key, value, ttl=ttl)

        assert _uploaded(transport.requests[0]) == written.read_bytes()
        assert _name(key) == written.name


@pytest.mark.unit
class TestDelete:
    def test_deleted(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=204, body=b""))

        assert backend.delete("k") is True
        assert transport.sent() == [("DELETE", f"/o/{_name('k')}")]

    def test_absent_object(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=404))
        assert backend.delete("k") is False


@pytest.mark.unit
class TestPrefix:
    def test_prefix_precedes_the_name_verbatim(self, transport: _Transport) -> None:
        backend = GCSBackend(GCSBackendConfig(bucket=BUCKET, prefix="cachekit/v1-"))
        transport.script(_Reply(status=404), _Reply(status=404), _Reply(status=404))

        backend.get("k")
        backend.exists("k")
        backend.delete("k")

        assert {path for _, path in transport.sent()} == {f"/o/cachekit/v1-{_name('k')}"}

    def test_prefix_is_not_a_key_prefix(self, transport: _Transport) -> None:
        """A storage location only: nothing reads it as part of the cache key or the AAD."""
        assert not hasattr(GCSBackend(GCSBackendConfig(bucket=BUCKET, prefix="p/")), "key_prefix")


_STATUS_CASES = [
    (401, BackendErrorType.AUTHENTICATION),
    (403, BackendErrorType.AUTHENTICATION),
    (429, BackendErrorType.TRANSIENT),
    (500, BackendErrorType.TRANSIENT),
    (502, BackendErrorType.TRANSIENT),
    (503, BackendErrorType.TRANSIENT),
    (504, BackendErrorType.TRANSIENT),
    (408, BackendErrorType.TIMEOUT),
    (400, BackendErrorType.PERMANENT),
    (412, BackendErrorType.PERMANENT),
]
_TRANSPORT_CASES = [
    (requests.ReadTimeout(), BackendErrorType.TIMEOUT),
    (requests.ConnectTimeout(), BackendErrorType.TIMEOUT),
    (TimeoutError(), BackendErrorType.TIMEOUT),
    (requests.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.")), BackendErrorType.TIMEOUT),  # type: ignore[arg-type]
    (requests.ConnectionError(), BackendErrorType.TRANSIENT),
    (requests.exceptions.ChunkedEncodingError(), BackendErrorType.TRANSIENT),
    (DataCorruption(None, "checksum mismatch"), BackendErrorType.TRANSIENT),
    (ConnectionResetError(), BackendErrorType.TRANSIENT),
    (RefreshError("token"), BackendErrorType.AUTHENTICATION),
    (DefaultCredentialsError("none"), BackendErrorType.AUTHENTICATION),
    (TransportError("metadata server"), BackendErrorType.TRANSIENT),
    (RefreshError(TransportError("metadata server")), BackendErrorType.TRANSIENT),
    (RefreshError("token endpoint busy", retryable=True), BackendErrorType.TRANSIENT),
    (ValueError("?"), BackendErrorType.UNKNOWN),
]
_OPERATIONS = {
    "get": lambda b: b.get("k"),
    "set": lambda b: b.set("k", b"v", ttl=60),
    "delete": lambda b: b.delete("k"),
    "exists": lambda b: b.exists("k"),
}


@pytest.mark.unit
class TestErrors:
    @pytest.mark.parametrize("operation", _OPERATIONS)
    @pytest.mark.parametrize("status,error_type", _STATUS_CASES)
    def test_http_status_mapping(
        self, backend: GCSBackend, transport: _Transport, operation: str, status: int, error_type: BackendErrorType
    ) -> None:
        transport.script(_Reply(status=status, body=b'{"error": {"message": "nope"}}'))

        with pytest.raises(BackendError) as exc_info:
            _OPERATIONS[operation](backend)

        assert exc_info.value.error_type is error_type
        assert exc_info.value.operation == operation
        assert len(transport.requests) == 1  # no retry

    @pytest.mark.parametrize("operation", _OPERATIONS)
    @pytest.mark.parametrize(
        "error,error_type", _TRANSPORT_CASES, ids=[f"{i}-{type(e).__name__}" for i, (e, _) in enumerate(_TRANSPORT_CASES)]
    )
    def test_transport_error_mapping(
        self, backend: GCSBackend, transport: _Transport, operation: str, error: Exception, error_type: BackendErrorType
    ) -> None:
        transport.script(_Reply(error=error))

        with pytest.raises(BackendError) as exc_info:
            _OPERATIONS[operation](backend)

        assert exc_info.value.error_type is error_type
        assert exc_info.value.original_exception is error
        assert len(transport.requests) == 1  # no retry

    def test_set_to_a_missing_bucket_is_permanent(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(status=404))

        with pytest.raises(BackendError) as exc_info:
            backend.set("k", b"v")
        assert exc_info.value.error_type is BackendErrorType.PERMANENT

    def test_message_carries_no_bucket_object_or_key(self, backend: GCSBackend, transport: _Transport) -> None:
        """Google's error text quotes the request URL; only the type and status reach str(e)."""
        transport.script(_Reply(status=403, body=b'{"error": {"message": "caller lacks access to cache-bucket"}}'))

        with pytest.raises(BackendError) as exc_info:
            backend.get("tenant-42:secret-key")

        text = str(exc_info.value)
        assert "Forbidden (HTTP 403)" in text
        for leak in (BUCKET, _name("tenant-42:secret-key"), "tenant-42", "caller lacks"):
            assert leak not in text

    def test_classifier_defaults(self) -> None:
        error = classify_gcs_error(KeyError("x"))
        assert error.error_type is BackendErrorType.UNKNOWN
        assert error.operation is None


@pytest.mark.unit
class TestHealthCheck:
    def test_healthy_is_one_object_listing(self, backend: GCSBackend, transport: _Transport) -> None:
        transport.script(_Reply(body=b'{"kind": "storage#objects"}'))

        healthy, details = backend.health_check()

        assert healthy is True
        assert details["backend_type"] == "gcs"
        assert details["bucket"] == BUCKET
        assert isinstance(details["latency_ms"], float)
        assert "error" not in details
        assert transport.sent() == [("GET", "/o")]
        assert parse_qs(urlsplit(transport.requests[0].url or "").query)["maxResults"] == ["1"]

    def test_listing_is_scoped_to_the_prefix(self, transport: _Transport) -> None:
        transport.script(_Reply(body=b"{}"))
        GCSBackend(GCSBackendConfig(bucket=BUCKET, prefix="p/")).health_check()
        assert parse_qs(urlsplit(transport.requests[0].url or "").query)["prefix"] == ["p/"]

    @pytest.mark.parametrize("reply", [_Reply(status=403), _Reply(status=404), _Reply(error=requests.ReadTimeout())])
    def test_unhealthy_reports_the_error_and_does_not_raise(
        self, backend: GCSBackend, transport: _Transport, reply: _Reply
    ) -> None:
        transport.script(reply)

        healthy, details = backend.health_check()

        assert healthy is False
        assert details["backend_type"] == "gcs"
        assert "latency_ms" in details
        assert "operation=health_check" in details["error"]


@pytest.mark.unit
class TestNoBucketLevelRequest:
    def test_no_operation_reads_bucket_metadata(
        self, backend: GCSBackend, transport: _Transport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every request is to an object or the object listing, so object-level IAM is enough.

        The client library would otherwise fetch the bucket itself in a background thread, on the
        first request and again after every 404. Its two entry points to that are recorded here,
        on the calling thread, so the test does not depend on when a background thread runs.
        """
        cache_module = pytest.importorskip("google.cloud.storage._bucket_metadata_cache")
        fetches: list[str] = []
        for entry_point in ("get_or_queue_fetch", "check_and_evict"):
            monkeypatch.setattr(cache_module.BucketMetadataCache, entry_point, lambda self, name: fetches.append(name))
        transport.script(
            _Reply(status=404),  # get: miss
            _Reply(),  # set
            _Reply(body=_header() + b"v"),  # get: hit
            _Reply(status=206, body=_header()),  # exists
            _Reply(status=204, body=b""),  # delete
            _Reply(status=404),  # delete: absent
            _Reply(body=b"{}"),  # health_check
        )

        backend.get("k")
        backend.set("k", b"v")
        backend.get("k")
        backend.exists("k")
        backend.delete("k")
        backend.delete("k")
        backend.health_check()

        assert fetches == []
        assert transport.replies == []
        assert all(path == "/o" or path.startswith("/o/") for _, path in transport.sent())


@pytest.mark.unit
class TestConstruction:
    def test_config_defaults_to_the_environment(self, transport: _Transport, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_GCS_BUCKET", BUCKET)
        monkeypatch.setenv("CACHEKIT_GCS_PREFIX", "env/")
        transport.script(_Reply(status=404))

        GCSBackend().get("k")

        assert transport.sent() == [("GET", f"/o/env/{_name('k')}")]

    def test_missing_library_names_the_extra(self) -> None:
        """In an interpreter where no ``google`` package can load: the imports work, the constructor explains."""
        script = (
            "import sys\n"
            "sys.modules['google'] = None\n"
            "import cachekit, cachekit.backends, cachekit.backends.gcs\n"
            "from cachekit.backends.gcs import GCSBackend, GCSBackendConfig\n"
            "try:\n"
            "    GCSBackend(GCSBackendConfig(bucket='cache-bucket'))\n"
            "except ImportError as exc:\n"
            "    print(exc)\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)  # noqa: S603

        assert result.returncode == 0, result.stderr
        assert "pip install 'cachekit[gcs]'" in result.stdout

    def test_missing_credentials_is_an_authentication_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_credentials(project: str | None) -> Any:
            raise DefaultCredentialsError("Your default credentials were not found.")

        monkeypatch.setattr("cachekit.backends.gcs.backend._build_client", no_credentials)

        with pytest.raises(BackendError) as exc_info:
            GCSBackend(GCSBackendConfig(bucket=BUCKET))
        assert exc_info.value.error_type is BackendErrorType.AUTHENTICATION

    @pytest.mark.parametrize("project", [None, "my-project"])
    def test_client_gets_the_configured_project_and_looks_for_no_other(
        self, monkeypatch: pytest.MonkeyPatch, project: str | None
    ) -> None:
        """No project configured means a client without one: object requests need none."""
        from cachekit.backends.gcs.backend import _build_client

        monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://localhost:1")  # anonymous credentials, nothing sent
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ambient-project")

        assert _build_client(project).project == project

    def test_timeout_reaches_the_transport(self, transport: _Transport) -> None:
        seen: list[Any] = []
        send = transport.send

        def recording_send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            seen.append(kwargs.get("timeout"))
            return send(request, **kwargs)

        transport.send = recording_send  # type: ignore[method-assign]
        backend = GCSBackend(GCSBackendConfig(bucket=BUCKET, timeout=2.5))
        transport.script(_Reply(status=404), _Reply(), _Reply(status=404), _Reply(status=404), _Reply(body=b"{}"))

        backend.get("k")
        backend.set("k", b"v")
        backend.exists("k")
        backend.delete("k")
        backend.health_check()

        assert seen == [2.5] * 5


def _serve(transport: _Transport, vector: dict[str, Any], ranged: bool) -> None:
    body = bytes.fromhex(vector["file_hex"])
    transport.script(_Reply(status=206 if ranged else 200, body=body[:14] if ranged else body, generation=1))


def _reader_clock(vector: dict[str, Any]) -> contextlib.AbstractContextManager[Any]:
    if "reader_now_unix_seconds" not in vector:
        return contextlib.nullcontext()
    return time_machine.travel(vector["reader_now_unix_seconds"], tick=False)


@pytest.mark.unit
@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
class TestProtocolVectors:
    """The protocol's file-backend vectors, served as objects (tests/unit/protocol/fixtures/file-backend.json)."""

    def test_object_name(self, backend: GCSBackend, transport: _Transport, vector: dict[str, Any]) -> None:
        transport.script(_Reply(status=404))
        backend.get(vector["key_utf8"])
        assert transport.sent() == [("GET", f"/o/{vector['filename']}")]

    def test_get(self, backend: GCSBackend, transport: _Transport, vector: dict[str, Any]) -> None:
        _serve(transport, vector, ranged=False)
        transport.script(_Reply(status=204, body=b""))  # for the delete, when the action is one

        with _reader_clock(vector):
            result = backend.get(vector["key_utf8"])

        self._check(transport, vector, result, miss=None, hit=bytes.fromhex(vector["payload_hex"]))

    def test_exists(self, backend: GCSBackend, transport: _Transport, vector: dict[str, Any]) -> None:
        _serve(transport, vector, ranged=True)
        transport.script(_Reply(status=204, body=b""))

        with _reader_clock(vector):
            result = backend.exists(vector["key_utf8"])

        self._check(transport, vector, result, miss=False, hit=True)

    def test_set_writes_the_vector_bytes(self, backend: GCSBackend, transport: _Transport, vector: dict[str, Any]) -> None:
        """A writer never sets the reserved byte or a flag, so only unflagged vectors can be written."""
        if vector["reader_action"] == "miss_preserve":
            pytest.skip("this writer emits no reserved byte or flag")
        expiry = vector["expiry_unix_seconds"]
        transport.script(_Reply())

        with time_machine.travel(expiry - 60 if expiry else 0, tick=False):
            backend.set(vector["key_utf8"], bytes.fromhex(vector["payload_hex"]), ttl=60 if expiry else None)

        assert _uploaded(transport.requests[0]).hex() == vector["file_hex"]

    @staticmethod
    def _check(transport: _Transport, vector: dict[str, Any], result: Any, miss: Any, hit: Any) -> None:
        methods = [method for method, _ in transport.sent()]
        action = vector["reader_action"]
        if action == "return_payload":
            assert result == hit
            assert methods == ["GET"]
        elif action == "miss_preserve":
            assert result is miss
            assert methods == ["GET"]  # not deleted, not rewritten
        else:
            assert action == "miss_expired"
            assert result is miss
            assert methods == ["GET", "DELETE"]
