"""Unit tests for backend resolution logic.

Tests 3-tier resolution:
- Tier 1: Explicit backend kwarg (highest priority)
- Tier 2: Module default via set_default_backend()
- Tier 3: REDIS_URL env var auto-creates RedisBackend
- Test fail-fast with helpful error when no backend configured
- Test set_default_backend() / get_default_backend() module API
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cachekit.config.decorator import (
    _resolve_backend,
    get_default_backend,
    set_default_backend,
)
from cachekit.config.validation import ConfigurationError

if TYPE_CHECKING:
    from cachekit.backends.base import BaseBackend


@pytest.mark.unit
class TestSetDefaultBackend:
    """Test set_default_backend() module API."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    def test_set_default_backend(self) -> None:
        """Test setting default backend."""
        mock_backend: BaseBackend = MagicMock()
        set_default_backend(mock_backend)
        assert get_default_backend() is mock_backend

    def test_set_default_backend_to_none(self) -> None:
        """Test clearing default backend."""
        mock_backend: BaseBackend = MagicMock()
        set_default_backend(mock_backend)
        assert get_default_backend() is mock_backend

        set_default_backend(None)
        assert get_default_backend() is None

    def test_get_default_backend_unset(self) -> None:
        """Test get_default_backend() returns None when unset."""
        set_default_backend(None)
        assert get_default_backend() is None


@pytest.mark.unit
class TestResolveBackendTier1:
    """Test Tier 1: Explicit backend parameter (highest priority)."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    def test_explicit_backend_overrides_default(self) -> None:
        """Test explicit backend parameter overrides module default."""
        default_backend: BaseBackend = MagicMock()
        explicit_backend: BaseBackend = MagicMock()

        set_default_backend(default_backend)
        resolved = _resolve_backend(explicit_backend)

        assert resolved is explicit_backend

    def test_explicit_backend_none_for_l1_only(self) -> None:
        """Test explicit backend=None enables L1-only mode."""
        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        resolved = _resolve_backend(None)
        assert resolved is None

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_explicit_backend_overrides_env_var(self) -> None:
        """Test explicit backend overrides REDIS_URL env var."""
        explicit_backend: BaseBackend = MagicMock()
        resolved = _resolve_backend(explicit_backend)
        assert resolved is explicit_backend


@pytest.mark.unit
class TestResolveBackendTier2:
    """Test Tier 2: Module-level default (set_default_backend)."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    @patch.dict(os.environ, {}, clear=True)
    def test_module_default_used_when_no_explicit_backend(self) -> None:
        """Test module default used when no explicit backend."""
        # Clear REDIS_URL env var
        os.environ.pop("REDIS_URL", None)

        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        resolved = _resolve_backend()
        assert resolved is default_backend

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_module_default_overrides_env_var(self) -> None:
        """Test module default has higher priority than REDIS_URL env var."""
        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        resolved = _resolve_backend()
        assert resolved is default_backend


@pytest.mark.unit
class TestResolveBackendTier3:
    """Test Tier 3: REDIS_URL env var auto-creates RedisBackend."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_redis_url_auto_creates_backend(self) -> None:
        """Test REDIS_URL env var auto-creates RedisBackend."""
        resolved = _resolve_backend()

        # Lazy import to match actual implementation
        from cachekit.backends.redis import RedisBackend

        assert isinstance(resolved, RedisBackend)

    @patch.dict(os.environ, {"REDIS_URL": "redis://custom-host:6380/2"})
    def test_redis_url_custom_url(self) -> None:
        """Test REDIS_URL with custom host/port/db."""
        resolved = _resolve_backend()

        # Lazy import to match actual implementation
        from cachekit.backends.redis import RedisBackend

        assert isinstance(resolved, RedisBackend)


@pytest.mark.unit
class TestResolveBackendFailFast:
    """Test fail-fast behavior when no backend configured."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    @patch.dict(os.environ, {}, clear=True)
    def test_no_backend_raises_configuration_error(self) -> None:
        """Test ConfigurationError raised when no backend configured."""
        # Clear REDIS_URL env var
        os.environ.pop("REDIS_URL", None)

        with pytest.raises(ConfigurationError, match="No backend configured"):
            _resolve_backend()

    @patch.dict(os.environ, {}, clear=True)
    def test_error_message_includes_redis_url_fix(self) -> None:
        """Test error message includes REDIS_URL quick fix."""
        # Clear REDIS_URL env var
        os.environ.pop("REDIS_URL", None)

        with pytest.raises(ConfigurationError, match="export REDIS_URL=redis://localhost:6379"):
            _resolve_backend()

    @patch.dict(os.environ, {}, clear=True)
    def test_error_message_includes_set_default_backend_fix(self) -> None:
        """Test error message includes set_default_backend() fix."""
        # Clear REDIS_URL env var
        os.environ.pop("REDIS_URL", None)

        with pytest.raises(ConfigurationError, match="set_default_backend"):
            _resolve_backend()

    @patch.dict(os.environ, {}, clear=True)
    def test_error_message_includes_l1_only_fix(self) -> None:
        """Test error message includes L1-only mode fix."""
        # Clear REDIS_URL env var
        os.environ.pop("REDIS_URL", None)

        with pytest.raises(ConfigurationError, match="@cache\\(backend=None\\)"):
            _resolve_backend()


@pytest.mark.unit
class TestResolveBackendPriority:
    """Test resolution priority across all 3 tiers."""

    def teardown_method(self) -> None:
        """Clear default backend after each test."""
        set_default_backend(None)

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_tier1_beats_tier2_beats_tier3(self) -> None:
        """Test explicit backend > module default > REDIS_URL."""
        # Setup all 3 tiers
        explicit_backend: BaseBackend = MagicMock(name="explicit")
        default_backend: BaseBackend = MagicMock(name="default")
        # REDIS_URL set in environment

        set_default_backend(default_backend)

        # Tier 1 (explicit) wins
        resolved = _resolve_backend(explicit_backend)
        assert resolved is explicit_backend

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_tier2_beats_tier3(self) -> None:
        """Test module default > REDIS_URL."""
        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        # Tier 2 (module default) wins
        resolved = _resolve_backend()
        assert resolved is default_backend

    @patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"})
    def test_tier3_only_when_no_tier1_or_tier2(self) -> None:
        """Test REDIS_URL only used when no explicit or module default."""
        # Tier 3 (REDIS_URL) used
        resolved = _resolve_backend()

        from cachekit.backends.redis import RedisBackend

        assert isinstance(resolved, RedisBackend)


@pytest.mark.unit
class TestResolveBackendUnsetSentinel:
    """Test _UNSET sentinel for explicit backend parameter."""

    def test_unset_sentinel_default_value(self) -> None:
        """Test _UNSET is the default value for explicit_backend."""
        # This test verifies the sentinel pattern works correctly
        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        # No explicit backend parameter (uses _UNSET default)
        resolved = _resolve_backend()
        assert resolved is default_backend

        # Cleanup
        set_default_backend(None)

    def test_unset_vs_none(self) -> None:
        """Test _UNSET (not provided) vs None (L1-only mode)."""
        default_backend: BaseBackend = MagicMock()
        set_default_backend(default_backend)

        # _UNSET means "not provided" -> use module default
        resolved_unset = _resolve_backend()
        assert resolved_unset is default_backend

        # None means "explicitly L1-only mode"
        resolved_none = _resolve_backend(None)
        assert resolved_none is None

        # Cleanup
        set_default_backend(None)
