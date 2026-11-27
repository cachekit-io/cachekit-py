"""Unit tests for GCSBackendConfig. They need no google-cloud-storage: the config imports none of it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cachekit.backends.gcs.config import MAX_PREFIX_BYTES, GCSBackendConfig


@pytest.fixture(autouse=True)
def _no_gcs_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CACHEKIT_GCS_BUCKET", "CACHEKIT_GCS_PREFIX", "CACHEKIT_GCS_PROJECT", "CACHEKIT_GCS_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
class TestGCSBackendConfig:
    def test_defaults(self) -> None:
        config = GCSBackendConfig(bucket="cache-bucket")

        assert (config.bucket, config.prefix, config.project, config.timeout) == ("cache-bucket", "", None, 10.0)

    def test_fields_are_exactly_these(self) -> None:
        """No credential field: authentication is Application Default Credentials only."""
        assert set(GCSBackendConfig.model_fields) == {"bucket", "prefix", "project", "timeout"}

    def test_bucket_is_required(self) -> None:
        with pytest.raises(ValidationError):
            GCSBackendConfig.from_env()

    @pytest.mark.parametrize("bucket", ["", "gs://cache-bucket", "cache-bucket/prefix"])
    def test_bucket_must_be_a_bare_name(self, bucket: str) -> None:
        with pytest.raises(ValidationError):
            GCSBackendConfig(bucket=bucket)

    @pytest.mark.parametrize("prefix", ["a\nb", "a\rb", "x" * (MAX_PREFIX_BYTES + 1), "é" * (MAX_PREFIX_BYTES // 2 + 1)])
    def test_prefix_gcs_would_refuse_is_rejected(self, prefix: str) -> None:
        with pytest.raises(ValidationError):
            GCSBackendConfig(bucket="cache-bucket", prefix=prefix)

    def test_longest_prefix_is_accepted(self) -> None:
        assert len(GCSBackendConfig(bucket="cache-bucket", prefix="x" * MAX_PREFIX_BYTES).prefix) == MAX_PREFIX_BYTES

    @pytest.mark.parametrize("timeout", [0, 0.05, 601])
    def test_timeout_bounds(self, timeout: float) -> None:
        with pytest.raises(ValidationError):
            GCSBackendConfig(bucket="cache-bucket", timeout=timeout)

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_GCS_BUCKET", "env-bucket")
        monkeypatch.setenv("CACHEKIT_GCS_PREFIX", "cachekit/")
        monkeypatch.setenv("CACHEKIT_GCS_PROJECT", "env-project")
        monkeypatch.setenv("CACHEKIT_GCS_TIMEOUT", "2.5")

        config = GCSBackendConfig.from_env()

        assert (config.bucket, config.prefix, config.project, config.timeout) == ("env-bucket", "cachekit/", "env-project", 2.5)

    def test_constructor_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_GCS_BUCKET", "env-bucket")
        assert GCSBackendConfig(bucket="explicit").bucket == "explicit"
