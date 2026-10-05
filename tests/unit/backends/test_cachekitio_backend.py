"""Unit tests for CachekitIOBackend sync methods.

Requests go through the backend's real HTTPClient on a fake pool (tests/utils/cachekitio_fakes.py), so path and
header building, status handling and error classification run as in production; only the socket is replaced.
Async methods mirror the same logic and are not duplicated here, apart from the 404 paths that differ by method;
every async method is driven in test_cachekitio_event_loops.py.
"""

from __future__ import annotations

import string
from typing import Any
from unittest.mock import patch

import pytest
from urllib3 import HTTPResponse

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.config.validation import ConfigurationError
from tests.utils.cachekitio_fakes import TEST_API_KEY, TEST_API_URL, FakePool, FakeRequest, fake_backend, response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Server:
    """Answers every request with ``answer``: a response, or an exception to raise as the transport would."""

    def __init__(self) -> None:
        self.answer: HTTPResponse | Exception = response(200)

    def __call__(self, request: FakeRequest) -> HTTPResponse:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def backend_and_pool(server: _Server) -> tuple[CachekitIOBackend, FakePool]:
    return fake_backend(server, api_url=TEST_API_URL, api_key=TEST_API_KEY)


@pytest.fixture
def backend(backend_and_pool: tuple[CachekitIOBackend, FakePool]) -> CachekitIOBackend:
    """CachekitIOBackend whose every request, sync or async, goes to ``server``."""
    return backend_and_pool[0]


@pytest.fixture
def pool(backend_and_pool: tuple[CachekitIOBackend, FakePool]) -> FakePool:
    return backend_and_pool[1]


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInit:
    """Tests for CachekitIOBackend.__init__."""

    def test_manual_config_accepted(self) -> None:
        """Both api_url and api_key provided: backend initialises cleanly."""
        b = CachekitIOBackend(api_url=TEST_API_URL, api_key=TEST_API_KEY)
        assert b._config.api_url == TEST_API_URL
        assert b._config.api_key.get_secret_value() == TEST_API_KEY

    def test_timeout_override_stored(self) -> None:
        """Explicit timeout is stored in config."""
        b = CachekitIOBackend(api_url=TEST_API_URL, api_key=TEST_API_KEY, timeout=30.0)
        assert b._config.timeout == 30.0

    def test_timeout_defaults_to_five(self) -> None:
        """Omitting timeout defaults to 5.0 seconds."""
        b = CachekitIOBackend(api_url=TEST_API_URL, api_key=TEST_API_KEY)
        assert b._config.timeout == 5.0

    def test_api_key_alone_fills_url_and_timeout_from_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """api_key without api_url is valid: the URL and timeout come from env / defaults."""
        monkeypatch.delenv("CACHEKIT_API_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_TIMEOUT", raising=False)
        b = CachekitIOBackend(api_key=TEST_API_KEY)
        assert b._config.api_key.get_secret_value() == TEST_API_KEY
        assert b._config.api_url == TEST_API_URL
        assert b._config.timeout == 5.0

    def test_api_key_argument_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit api_key wins over CACHEKIT_API_KEY."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env_key")  # pragma: allowlist secret
        b = CachekitIOBackend(api_key=TEST_API_KEY)
        assert b._config.api_key.get_secret_value() == TEST_API_KEY

    def test_no_key_anywhere_raises_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither api_key nor CACHEKIT_API_KEY: ConfigurationError here, not a 401 on the first call."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="api_key"):
            CachekitIOBackend(api_url=TEST_API_URL)

    def test_empty_key_raises_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty key would go out as 'Bearer ' — reject it where the preset is built."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="api_key"):
            CachekitIOBackend(api_key="")

    @pytest.mark.parametrize(
        "key",
        [
            "   ",
            "ck_live_SECRET_XYZ\n",  # pragma: allowlist secret
            "ck_live_SECRET XYZ",  # pragma: allowlist secret
            "\ufeffck_live_SECRET_XYZ",  # pragma: allowlist secret
            "ck_live_SECRET\x00XYZ",  # pragma: allowlist secret
            "ck_live_SECRET\x7fXYZ",  # pragma: allowlist secret
            "ck_live_SECRET\u00e9XYZ",  # pragma: allowlist secret
        ],
        ids=["blank", "newline", "inner-space", "bom", "nul", "del", "non-ascii-letter"],
    )
    def test_non_token_key_raises_at_construction(self, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
        """Outside the RFC 6750 charset a key fails later with the key in the error: on the first request, a newline as
        http.client's ValueError quoting the header value, a BOM as a UnicodeEncodeError whose repr holds "Bearer <key>".
        Reject at validation, and echo none of it."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="not a valid bearer token") as info:
            CachekitIOBackend(api_key=key)
        assert "SECRET" not in str(info.value)
        assert "SECRET" not in repr(info.value)
        assert "requires an API key" not in str(info.value)  # a key WAS given

    @pytest.mark.parametrize(
        "key",
        [
            "ck_live_" + string.ascii_letters + string.digits,  # pragma: allowlist secret
            "ck_sdk_" + string.ascii_letters + string.digits,  # pragma: allowlist secret
            "ck_api_" + string.ascii_letters + string.digits,  # pragma: allowlist secret
            TEST_API_KEY,
            "AZaz09-._~+/==",  # pragma: allowlist secret
        ],
        ids=["live", "sdk", "api", "test", "every-b64token-char"],
    )
    def test_bearer_token_key_validates(self, key: str) -> None:
        """Issued keys are an ASCII prefix plus letters and digits; the charset must not narrow below RFC 6750."""
        assert CachekitIOBackendConfig(api_key=key).api_key.get_secret_value() == key

    @pytest.mark.parametrize(
        ("env", "kwargs", "loc"),
        [
            ({}, {"api_key": "ck_live_SECRET_XYZ\n"}, ("api_key",)),  # pragma: allowlist secret
            ({}, {"api_key": "\ufeffck_live_SECRET_XYZ"}, ("api_key",)),  # pragma: allowlist secret
            ({}, {"api_key": "ck_live_SECRET_XYZ", "api_url": "https://evil.example.com"}, ()),  # pragma: allowlist secret
            (
                {
                    "CACHEKIT_API_KEY": "ck_live_SECRET_XYZ",  # pragma: allowlist secret
                    "CACHEKIT_API_URL": "https://staging.internal.example",
                },
                None,
                (),
            ),
            (
                {},
                {"api_key": TEST_API_KEY, "api_url": "https://user:SECRET_PW@api.cachekit.io"},  # pragma: allowlist secret
                ("api_url",),
            ),
        ],
        ids=["whitespace", "bom", "allowlist", "from-env-allowlist", "userinfo"],
    )
    def test_public_config_class_never_prints_the_key(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], kwargs: dict[str, str] | None, loc: tuple[str, ...]
    ) -> None:
        """CWE-532: CachekitIOBackendConfig is public; built directly, no surface of its ValidationError may carry the
        key or URL credentials. hide_input_in_errors covers str()/repr() only; errors()/json() are what error trackers
        and API error handlers serialize, and the chain would still hold the original error."""
        from pydantic import ValidationError

        monkeypatch.delenv("CACHEKIT_ALLOW_CUSTOM_HOST", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError) as info:
            CachekitIOBackendConfig.from_env() if kwargs is None else CachekitIOBackendConfig(**kwargs)
        exc = info.value
        for rendered in (str(exc), repr(exc), exc.json(), repr(exc.errors())):
            assert "SECRET" not in rendered
        assert [err["input"] for err in exc.errors()] == ["[REDACTED]"]
        assert exc.errors()[0]["loc"] == loc  # unchanged: CachekitIOBackend's api_key hint keys on it
        assert exc.__context__ is None
        assert exc.__cause__ is None

    def test_unparseable_url_error_carries_no_credentials(self) -> None:
        """CWE-532: the message once held the whole URL, and urlparse's own error (NFKC-invalid netloc)
        quotes userinfo too, so neither may reach the message or the chain pydantic keeps in ctx."""
        from pydantic import ValidationError

        url = "https://user:SECRET_PW\uff0fx@api.cachekit.io"  # pragma: allowlist secret
        with pytest.raises(ValidationError) as info:
            CachekitIOBackendConfig(api_key=TEST_API_KEY, api_url=url)
        error = info.value.errors(include_input=False)[0]["ctx"]["error"]
        assert "SECRET" not in str(error)
        assert error.__cause__ is None
        assert error.__context__ is None

    @pytest.mark.parametrize("userinfo", ["user:SECRET_PW@", "SECRET_USER@"], ids=["user-password", "user-only"])
    def test_url_with_userinfo_is_rejected(self, userinfo: str) -> None:
        """URL userinfo never authenticates (the Bearer key is the only credential) and is one more place for a
        password to reach logs (CWE-532): reject it."""
        with pytest.raises(ConfigurationError, match="must not contain credentials") as info:
            CachekitIOBackend(api_key=TEST_API_KEY, api_url=f"https://{userinfo}api.cachekit.io")
        assert "SECRET" not in str(info.value)

    def test_https_error_never_echoes_a_schemeless_url(self) -> None:
        """Without a scheme, urlparse reads the username as one, and the HTTPS error once echoed it."""
        url = "SECRETUSER:pw@api.cachekit.io"  # pragma: allowlist secret
        with pytest.raises(ConfigurationError, match="must use HTTPS") as info:
            CachekitIOBackend(api_key=TEST_API_KEY, api_url=url)
        assert "secretuser" not in str(info.value).lower()  # urlparse lowercases the scheme

    def test_config_error_never_echoes_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CWE-532: a rejected api_url must not carry the key into the exception text or its chain."""
        monkeypatch.delenv("CACHEKIT_ALLOW_CUSTOM_HOST", raising=False)
        with pytest.raises(ConfigurationError, match="not in allowlist") as info:
            CachekitIOBackend(api_key="ck_live_SECRET_XYZ", api_url="https://evil.example.com")  # pragma: allowlist secret
        assert "SECRET" not in str(info.value)
        assert "requires an API key" not in str(info.value)  # the key hint is for key errors only
        # No chain at all: __context__ would still hold the ValidationError, whose .errors() carry the key.
        assert info.value.__cause__ is None
        assert info.value.__context__ is None

    def test_env_based_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All-None args triggers env-based config load."""
        monkeypatch.setenv("CACHEKIT_API_KEY", TEST_API_KEY)
        b = CachekitIOBackend()
        assert b._config.api_key.get_secret_value() == TEST_API_KEY


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGet:
    """Tests for CachekitIOBackend.get()."""

    def test_cache_hit_returns_bytes(self, backend: CachekitIOBackend, server: _Server, pool: FakePool) -> None:
        """200 response returns the response body bytes."""
        payload = b"cached-value"
        server.answer = response(200, payload)

        result = backend.get("my-key")

        assert result == payload
        assert len(pool.requests) == 1
        assert pool.requests[0].method == "GET"
        assert pool.requests[0].path == "/v1/cache/my-key"

    def test_cache_miss_returns_none(self, backend: CachekitIOBackend, server: _Server) -> None:
        """404 is a cache miss: None."""
        server.answer = response(404)
        assert backend.get("missing-key") is None

    def test_non_404_error_reraises(self, backend: CachekitIOBackend, server: _Server) -> None:
        """500 BackendError propagates from get()."""
        server.answer = response(500)
        with pytest.raises(BackendError) as exc_info:
            backend.get("bad-key")
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT


# ---------------------------------------------------------------------------
# TestSet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSet:
    """Tests for CachekitIOBackend.set()."""

    def test_set_sends_put(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """set() issues a PUT request."""
        backend.set("cache-key", b"data")

        assert pool.requests[0].method == "PUT"
        assert pool.requests[0].path == "/v1/cache/cache-key"

    def test_set_with_ttl_sends_canonical_ttl_header_only(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """When ttl is provided, X-CacheKit-TTL is sent and the legacy X-TTL is not (API-44)."""
        backend.set("cache-key", b"data", ttl=300)

        headers = pool.requests[0].headers
        assert headers["X-CacheKit-TTL"] == "300"
        assert "X-TTL" not in headers

    def test_set_without_ttl_omits_ttl_headers(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """When ttl is None, no TTL header is present."""
        backend.set("cache-key", b"data", ttl=None)

        assert "X-CacheKit-TTL" not in pool.requests[0].headers
        assert "X-TTL" not in pool.requests[0].headers

    def test_set_passes_body_bytes(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """set() forwards the value bytes as the request body."""
        payload = b"\x00\x01\x02binary-data"

        backend.set("cache-key", payload)

        assert pool.requests[0].body == payload


# ---------------------------------------------------------------------------
# TestDelete
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDelete:
    """Tests for CachekitIOBackend.delete()."""

    def test_delete_success_returns_true(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """Successful DELETE returns True."""
        result = backend.delete("del-key")

        assert result is True
        assert pool.requests[0].method == "DELETE"

    def test_delete_404_raises(self, backend: CachekitIOBackend, server: _Server) -> None:
        """The server answers DELETE with 200 whether or not the key existed, so a 404 is not a miss."""
        server.answer = response(404)
        with pytest.raises(BackendError) as exc_info:
            backend.delete("missing-key")
        assert exc_info.value.error_type == BackendErrorType.PERMANENT

    async def test_delete_async_404_raises(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Async twin of test_delete_404_raises."""
        server.answer = response(404)
        with pytest.raises(BackendError) as exc_info:
            await backend.delete_async("missing-key")
        assert exc_info.value.error_type == BackendErrorType.PERMANENT

    def test_delete_server_error_reraises(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Non-404 BackendError from DELETE propagates."""
        server.answer = response(503)  # no Retry-After: not the one shed-write retry
        with pytest.raises(BackendError) as exc_info:
            backend.delete("del-key")
        assert exc_info.value.error_type == BackendErrorType.TRANSIENT


# ---------------------------------------------------------------------------
# TestExists
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExists:
    """Tests for CachekitIOBackend.exists()."""

    def test_exists_uses_head_method(self, backend: CachekitIOBackend, pool: FakePool) -> None:
        """exists() issues a HEAD request, not GET."""
        result = backend.exists("some-key")

        assert result is True
        assert pool.requests[0].method == "HEAD"

    def test_exists_true_on_200(self, backend: CachekitIOBackend) -> None:
        """200 response means key exists."""
        assert backend.exists("some-key") is True

    def test_exists_false_on_404(self, backend: CachekitIOBackend, server: _Server) -> None:
        """404 means the key does not exist."""
        server.answer = response(404)
        assert backend.exists("missing-key") is False

    def test_exists_reraises_non_404(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Non-404 BackendError from exists() propagates."""
        server.answer = response(401)
        with pytest.raises(BackendError) as exc_info:
            backend.exists("auth-key")
        assert exc_info.value.error_type == BackendErrorType.AUTHENTICATION


# ---------------------------------------------------------------------------
# TestHealthCheck
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthCheck:
    """Tests for CachekitIOBackend.health_check()."""

    def test_healthy_returns_true_with_details(self, backend: CachekitIOBackend, server: _Server, pool: FakePool) -> None:
        """Successful health check returns (True, details) with expected keys."""
        server.answer = response(200, json={"version": "1.2.3"})

        healthy, details = backend.health_check()

        assert healthy is True
        assert details["backend_type"] == "saas"
        assert "latency_ms" in details
        assert details["latency_ms"] >= 0
        assert details["version"] == "1.2.3"
        assert "api_url" in details
        assert (pool.requests[0].method, pool.requests[0].path) == ("GET", "/v1/cache/health")

    def test_healthy_latency_is_numeric(self, backend: CachekitIOBackend, server: _Server) -> None:
        """latency_ms in healthy response is a non-negative number."""
        server.answer = response(200, json={})

        _, details = backend.health_check()

        assert isinstance(details["latency_ms"], (int, float))
        assert details["latency_ms"] >= 0

    def test_unhealthy_returns_false_with_error(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Backend error during health check returns (False, details) with error info."""
        server.answer = RuntimeError("connection refused")

        healthy, details = backend.health_check()

        assert healthy is False
        assert details["backend_type"] == "saas"
        assert details["latency_ms"] == -1
        assert "error" in details
        assert "error_type" in details

    def test_unhealthy_error_type_is_exception_class_name(self, backend: CachekitIOBackend, server: _Server) -> None:
        """error_type in failure details is the exception class name of what health_check catches.

        _request_sync wraps all exceptions in BackendError, so health_check() sees BackendError.
        """
        server.answer = RuntimeError("timeout")

        _, details = backend.health_check()

        # _request_sync converts RuntimeError -> BackendError before health_check sees it
        assert details["error_type"] == "BackendError"

    def test_health_check_version_defaults_to_unknown(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Missing version in response body defaults to 'unknown'."""
        server.answer = response(200, json={})

        _, details = backend.health_check()

        assert details["version"] == "unknown"


# ---------------------------------------------------------------------------
# TestWithTimeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWithTimeout:
    """Tests for CachekitIOBackend.with_timeout()."""

    def test_returns_new_instance(self, backend: CachekitIOBackend) -> None:
        """with_timeout() returns a different CachekitIOBackend object."""
        new_backend = backend.with_timeout(10.0)
        assert new_backend is not backend

    def test_new_instance_has_updated_timeout(self, backend: CachekitIOBackend) -> None:
        """New backend instance has the requested timeout."""
        new_backend = backend.with_timeout(42.0)
        assert new_backend._config.timeout == 42.0

    def test_original_instance_unchanged(self, backend: CachekitIOBackend) -> None:
        """Original backend timeout is unaffected by with_timeout()."""
        original_timeout = backend._config.timeout
        backend.with_timeout(99.0)
        assert backend._config.timeout == original_timeout

    def test_preserves_api_url_and_key(self, backend: CachekitIOBackend) -> None:
        """New instance preserves the same api_url and api_key."""
        new_backend = backend.with_timeout(7.0)
        assert new_backend._config.api_url == backend._config.api_url
        assert new_backend._config.api_key.get_secret_value() == backend._config.api_key.get_secret_value()


# ---------------------------------------------------------------------------
# TestRequestSyncErrorClassification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestMissWithoutException
# ---------------------------------------------------------------------------


_MISS_CALLS = [
    ("get", None),
    ("get_with_freshness", None),
    ("exists", False),
]


@pytest.mark.unit
class TestMissWithoutException:
    """A 404 on a key read or exists returns the miss without raising (LAB-7066).

    The miss path must never build HTTPStatusError or a BackendError: that round-trip
    is pure CPU on every cache miss.
    """

    @pytest.mark.parametrize(("method", "expected"), _MISS_CALLS)
    def test_sync_miss_skips_error_path(self, backend: CachekitIOBackend, server: _Server, method: str, expected: Any) -> None:
        server.answer = response(404)
        with patch("cachekit.backends.cachekitio.backend.classify_http_error") as classify:
            assert getattr(backend, method)("missing-key") is expected
        classify.assert_not_called()

    @pytest.mark.parametrize(("method", "expected"), [(m, e) for m, e in _MISS_CALLS if m != "get_with_freshness"])
    async def test_async_miss_skips_error_path(
        self, backend: CachekitIOBackend, server: _Server, method: str, expected: Any
    ) -> None:
        server.answer = response(404)
        with patch("cachekit.backends.cachekitio.backend.classify_http_error") as classify:
            assert await getattr(backend, f"{method}_async")("missing-key") is expected
        classify.assert_not_called()

    def test_404_outside_miss_paths_still_raises(self, backend: CachekitIOBackend, server: _Server) -> None:
        """Only the opted-in callers treat 404 as a miss; a 404 on PUT stays a classified error."""
        server.answer = response(404)
        with pytest.raises(BackendError) as exc_info:
            backend.set("some-key", b"data")
        assert exc_info.value.error_type == BackendErrorType.PERMANENT
