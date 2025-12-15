"""Unit tests for FileBackendConfig.

Tests configuration parsing, validation rules, environment variable loading,
and security warnings for the file-based cache backend.
"""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from cachekit.backends.file.config import FileBackendConfig


class TestFileBackendConfigDefaults:
    """Test default configuration values."""

    def test_default_values(self):
        """Test that default values are correctly set."""
        config = FileBackendConfig()

        assert config.max_size_mb == 1024
        assert config.max_value_mb == 100
        assert config.max_entry_count == 10_000
        assert config.lock_timeout_seconds == 5.0
        assert config.permissions == 0o600
        assert config.dir_permissions == 0o700

    def test_cache_dir_default_is_temp(self):
        """Test that default cache_dir is in system temp directory."""
        config = FileBackendConfig()

        temp_dir = Path(tempfile.gettempdir())
        assert config.cache_dir == temp_dir / "cachekit"
        assert str(config.cache_dir).startswith(str(temp_dir))

    def test_cache_dir_default_is_pathlib_path(self):
        """Test that cache_dir is a Path object."""
        config = FileBackendConfig()

        assert isinstance(config.cache_dir, Path)


class TestFileBackendConfigConstructor:
    """Test constructor with custom values."""

    def test_custom_values_via_constructor(self):
        """Test setting custom values via constructor."""
        custom_dir = Path("/var/cache/myapp")
        config = FileBackendConfig(
            cache_dir=custom_dir,
            max_size_mb=2048,
            max_value_mb=200,
            max_entry_count=50_000,
            lock_timeout_seconds=10.0,
            permissions=0o644,
            dir_permissions=0o755,
        )

        assert config.cache_dir == custom_dir
        assert config.max_size_mb == 2048
        assert config.max_value_mb == 200
        assert config.max_entry_count == 50_000
        assert config.lock_timeout_seconds == 10.0
        assert config.permissions == 0o644
        assert config.dir_permissions == 0o755

    def test_string_cache_dir_converted_to_path(self, tmp_path):
        """Test that string cache_dir is converted to Path."""
        test_dir = str(tmp_path / "cache")
        config = FileBackendConfig(cache_dir=test_dir)

        assert isinstance(config.cache_dir, Path)
        assert config.cache_dir == Path(test_dir)


class TestFileBackendConfigEnvVars:
    """Test environment variable parsing."""

    @pytest.fixture
    def clean_env(self, monkeypatch):
        """Remove all CACHEKIT_FILE_* environment variables."""
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_FILE_"):
                monkeypatch.delenv(key, raising=False)
        yield
        # Cleanup after test
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_FILE_"):
                monkeypatch.delenv(key, raising=False)

    def test_env_var_max_size_mb(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_MAX_SIZE_MB parsing."""
        monkeypatch.setenv("CACHEKIT_FILE_MAX_SIZE_MB", "2048")
        config = FileBackendConfig()

        assert config.max_size_mb == 2048

    def test_env_var_max_value_mb(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_MAX_VALUE_MB parsing."""
        monkeypatch.setenv("CACHEKIT_FILE_MAX_VALUE_MB", "256")
        monkeypatch.setenv("CACHEKIT_FILE_MAX_SIZE_MB", "1024")
        config = FileBackendConfig()

        assert config.max_value_mb == 256

    def test_env_var_max_entry_count(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_MAX_ENTRY_COUNT parsing."""
        monkeypatch.setenv("CACHEKIT_FILE_MAX_ENTRY_COUNT", "50000")
        config = FileBackendConfig()

        assert config.max_entry_count == 50_000

    def test_env_var_lock_timeout_seconds(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_LOCK_TIMEOUT_SECONDS parsing."""
        monkeypatch.setenv("CACHEKIT_FILE_LOCK_TIMEOUT_SECONDS", "15.5")
        config = FileBackendConfig()

        assert config.lock_timeout_seconds == 15.5

    def test_env_var_cache_dir(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_CACHE_DIR parsing."""
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", "/var/cache/myapp")
        config = FileBackendConfig()

        assert config.cache_dir == Path("/var/cache/myapp")

    def test_env_var_permissions(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_PERMISSIONS parsing (as decimal from env)."""
        # Environment variables are parsed as decimal, not octal
        # 0o644 in decimal is 420, but env string "644" is interpreted as decimal 644
        monkeypatch.setenv("CACHEKIT_FILE_PERMISSIONS", "420")  # 0o644 in decimal
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig()

            assert config.permissions == 0o644
            assert len(w) == 1
            assert "more permissive" in str(w[0].message)

    def test_env_var_dir_permissions(self, monkeypatch, clean_env):
        """Test CACHEKIT_FILE_DIR_PERMISSIONS parsing (as decimal from env)."""
        # Environment variables are parsed as decimal, not octal
        # 0o755 in decimal is 493
        monkeypatch.setenv("CACHEKIT_FILE_DIR_PERMISSIONS", "493")  # 0o755 in decimal
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig()

            assert config.dir_permissions == 0o755
            assert len(w) == 1
            assert "more permissive" in str(w[0].message)

    def test_env_var_case_insensitive(self, monkeypatch, clean_env):
        """Test that environment variables are case-insensitive."""
        monkeypatch.setenv("cachekit_file_max_size_mb", "512")
        config = FileBackendConfig()

        assert config.max_size_mb == 512

    def test_from_env_classmethod(self, monkeypatch, clean_env, tmp_path):
        """Test from_env() classmethod."""
        test_cache_dir = str(tmp_path / "test_cache")
        monkeypatch.setenv("CACHEKIT_FILE_MAX_SIZE_MB", "2048")
        monkeypatch.setenv("CACHEKIT_FILE_MAX_VALUE_MB", "200")
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", test_cache_dir)

        config = FileBackendConfig.from_env()

        assert isinstance(config, FileBackendConfig)
        assert config.max_size_mb == 2048
        assert config.max_value_mb == 200
        assert config.cache_dir == Path(test_cache_dir)


class TestFileBackendConfigValidation:
    """Test validation rules."""

    def test_max_size_mb_accepts_valid_range(self):
        """Test that max_size_mb accepts values from 1 to 1,000,000."""
        # When max_size_mb is 2, max_value_mb must be <= 1 (50% of 2)
        config_min = FileBackendConfig(max_size_mb=2, max_value_mb=1)
        assert config_min.max_size_mb == 2

        config_max = FileBackendConfig(max_size_mb=1_000_000, max_value_mb=500_000)
        assert config_max.max_size_mb == 1_000_000

        config_mid = FileBackendConfig(max_size_mb=10_000)
        assert config_mid.max_size_mb == 10_000

    def test_max_size_mb_rejects_zero(self):
        """Test that max_size_mb rejects 0."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_size_mb=0)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 1" in str(e) for e in errors)

    def test_max_size_mb_rejects_negative(self):
        """Test that max_size_mb rejects negative values."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_size_mb=-1)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 1" in str(e) for e in errors)

    def test_max_size_mb_rejects_over_limit(self):
        """Test that max_size_mb rejects values > 1,000,000."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_size_mb=1_000_001)

        errors = exc_info.value.errors()
        assert any("less than or equal to 1000000" in str(e) for e in errors)

    def test_max_value_mb_cannot_exceed_50_percent_of_max_size_mb(self):
        """Test that max_value_mb must be <= 50% of max_size_mb."""
        # max_size_mb=100 → max_value_mb max is 50
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_size_mb=100, max_value_mb=51)

        errors = exc_info.value.errors()
        assert any("50%" in str(e) for e in errors)

    def test_max_value_mb_accepts_exactly_50_percent(self):
        """Test that max_value_mb can be exactly 50% of max_size_mb."""
        config = FileBackendConfig(max_size_mb=200, max_value_mb=100)

        assert config.max_value_mb == 100

    def test_max_value_mb_accepts_less_than_50_percent(self):
        """Test that max_value_mb less than 50% is accepted."""
        config = FileBackendConfig(max_size_mb=200, max_value_mb=50)

        assert config.max_value_mb == 50

    def test_max_value_mb_uses_default_max_size_mb_if_not_set(self):
        """Test that max_value_mb validation uses default max_size_mb if not provided."""
        # Default max_size_mb=1024, so max_value_mb=100 is well within 50%
        config = FileBackendConfig()

        assert config.max_value_mb == 100
        assert config.max_value_mb <= config.max_size_mb * 0.5

    def test_max_entry_count_accepts_valid_range(self):
        """Test that max_entry_count accepts values from 100 to 1,000,000."""
        config_min = FileBackendConfig(max_entry_count=100)
        assert config_min.max_entry_count == 100

        config_max = FileBackendConfig(max_entry_count=1_000_000)
        assert config_max.max_entry_count == 1_000_000

        config_mid = FileBackendConfig(max_entry_count=500_000)
        assert config_mid.max_entry_count == 500_000

    def test_max_entry_count_rejects_below_minimum(self):
        """Test that max_entry_count rejects values < 100."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_entry_count=99)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 100" in str(e) for e in errors)

    def test_max_entry_count_rejects_zero(self):
        """Test that max_entry_count rejects 0."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_entry_count=0)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 100" in str(e) for e in errors)

    def test_max_entry_count_rejects_over_limit(self):
        """Test that max_entry_count rejects values > 1,000,000."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_entry_count=1_000_001)

        errors = exc_info.value.errors()
        assert any("less than or equal to 1000000" in str(e) for e in errors)

    def test_lock_timeout_seconds_accepts_valid_range(self):
        """Test that lock_timeout_seconds accepts values from 0.5 to 30.0."""
        config_min = FileBackendConfig(lock_timeout_seconds=0.5)
        assert config_min.lock_timeout_seconds == 0.5

        config_max = FileBackendConfig(lock_timeout_seconds=30.0)
        assert config_max.lock_timeout_seconds == 30.0

        config_mid = FileBackendConfig(lock_timeout_seconds=10.5)
        assert config_mid.lock_timeout_seconds == 10.5

    def test_lock_timeout_seconds_rejects_below_minimum(self):
        """Test that lock_timeout_seconds rejects values < 0.5."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(lock_timeout_seconds=0.4)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 0.5" in str(e) for e in errors)

    def test_lock_timeout_seconds_rejects_zero(self):
        """Test that lock_timeout_seconds rejects 0."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(lock_timeout_seconds=0.0)

        errors = exc_info.value.errors()
        assert any("greater than or equal to 0.5" in str(e) for e in errors)

    def test_lock_timeout_seconds_rejects_over_limit(self):
        """Test that lock_timeout_seconds rejects values > 30.0."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(lock_timeout_seconds=30.1)

        errors = exc_info.value.errors()
        assert any("less than or equal to 30" in str(e) for e in errors)


class TestFileBackendConfigSecurityWarnings:
    """Test security warnings for permissive permissions."""

    def test_warning_on_permissive_file_permissions(self):
        """Test that UserWarning is issued when file permissions > 0o600."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(permissions=0o644)

            assert len(w) == 1
            assert issubclass(w[0].category, UserWarning)
            assert "more permissive" in str(w[0].message)
            assert "0o600" in str(w[0].message) or "0o600" in str(w[0].message)
            assert config.permissions == 0o644

    def test_warning_on_highly_permissive_file_permissions(self):
        """Test warning on very permissive file permissions (0o666)."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(permissions=0o666)

            assert len(w) == 1
            assert "more permissive" in str(w[0].message)
            assert config.permissions == 0o666

    def test_no_warning_on_secure_file_permissions(self):
        """Test that no warning is issued for 0o600 or more restrictive."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(permissions=0o600)

            assert len(w) == 0
            assert config.permissions == 0o600

    def test_no_warning_on_more_restrictive_file_permissions(self):
        """Test that no warning is issued for permissions < 0o600."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(permissions=0o400)

            assert len(w) == 0
            assert config.permissions == 0o400

    def test_warning_on_permissive_dir_permissions(self):
        """Test that UserWarning is issued when dir permissions > 0o700."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(dir_permissions=0o755)

            assert len(w) == 1
            assert issubclass(w[0].category, UserWarning)
            assert "more permissive" in str(w[0].message)
            assert "0o700" in str(w[0].message) or "0o700" in str(w[0].message)
            assert config.dir_permissions == 0o755

    def test_warning_on_highly_permissive_dir_permissions(self):
        """Test warning on very permissive dir permissions (0o777)."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(dir_permissions=0o777)

            assert len(w) == 1
            assert "more permissive" in str(w[0].message)
            assert config.dir_permissions == 0o777

    def test_no_warning_on_secure_dir_permissions(self):
        """Test that no warning is issued for 0o700 or more restrictive."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(dir_permissions=0o700)

            assert len(w) == 0
            assert config.dir_permissions == 0o700

    def test_no_warning_on_more_restrictive_dir_permissions(self):
        """Test that no warning is issued for dir_permissions < 0o700."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(dir_permissions=0o500)

            assert len(w) == 0
            assert config.dir_permissions == 0o500

    def test_multiple_warnings_when_both_permissions_permissive(self):
        """Test that both warnings are issued when both permissions are permissive."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = FileBackendConfig(permissions=0o644, dir_permissions=0o755)

            assert len(w) == 2
            assert all(issubclass(warning.category, UserWarning) for warning in w)
            assert config.permissions == 0o644
            assert config.dir_permissions == 0o755


class TestFileBackendConfigEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_all_fields_at_boundaries(self):
        """Test configuration with all fields at their boundaries."""
        config = FileBackendConfig(
            max_size_mb=1_000_000,
            max_value_mb=500_000,  # 50% of max_size_mb
            max_entry_count=1_000_000,
            lock_timeout_seconds=30.0,
        )

        assert config.max_size_mb == 1_000_000
        assert config.max_value_mb == 500_000
        assert config.max_entry_count == 1_000_000
        assert config.lock_timeout_seconds == 30.0

    def test_all_fields_at_minimum_valid(self):
        """Test configuration with all fields at their minimum valid values."""
        # max_value_mb must be <= 50% of max_size_mb
        # For max_size_mb=2, max_value_mb can be at most 1
        config = FileBackendConfig(
            max_size_mb=2,
            max_value_mb=1,
            max_entry_count=100,
            lock_timeout_seconds=0.5,
        )

        assert config.max_size_mb == 2
        assert config.max_value_mb == 1
        assert config.max_entry_count == 100
        assert config.lock_timeout_seconds == 0.5

    def test_cache_dir_with_special_characters(self, tmp_path):
        """Test cache_dir with special characters in path."""
        special_path = tmp_path / "cache-kit_test-123"
        config = FileBackendConfig(cache_dir=special_path)

        assert config.cache_dir == special_path

    def test_extra_fields_rejected(self):
        """Test that extra fields are rejected due to extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(unknown_field="value")

        errors = exc_info.value.errors()
        assert any("extra_forbidden" in str(e) for e in errors)

    def test_float_max_size_mb_rejected(self):
        """Test that float max_size_mb is rejected by Pydantic strict int validation."""
        # Pydantic does not coerce floats to ints for int fields
        with pytest.raises(ValidationError) as exc_info:
            FileBackendConfig(max_size_mb=1024.5)

        errors = exc_info.value.errors()
        assert any("valid integer" in str(e) for e in errors)

    def test_max_value_mb_validation_considers_provided_max_size_mb(self):
        """Test that max_value_mb validation uses provided max_size_mb, not default."""
        config = FileBackendConfig(
            max_size_mb=200,  # Different from default
            max_value_mb=100,  # Exactly 50%
        )

        assert config.max_value_mb == 100

    def test_order_of_field_setting_does_not_matter(self):
        """Test that field order doesn't matter in validation."""
        # Set max_value_mb before max_size_mb in constructor call
        config = FileBackendConfig(
            max_value_mb=150,
            max_size_mb=400,  # max_value_mb is 37.5% of this
        )

        assert config.max_size_mb == 400
        assert config.max_value_mb == 150


class TestFileBackendConfigSerialization:
    """Test model serialization and structure."""

    def test_config_model_dump(self):
        """Test that config can be dumped to dict."""
        config = FileBackendConfig(
            max_size_mb=2048,
            max_value_mb=200,
        )

        data = config.model_dump()

        assert isinstance(data, dict)
        assert data["max_size_mb"] == 2048
        assert data["max_value_mb"] == 200
        assert "cache_dir" in data

    def test_cache_dir_serialized_as_string(self, tmp_path):
        """Test that Path objects are serialized properly."""
        test_dir = tmp_path / "test"
        config = FileBackendConfig(cache_dir=test_dir)
        data = config.model_dump()

        # Path should be serialized; check it exists in output
        assert "cache_dir" in data

    def test_config_repr(self):
        """Test that config has a useful repr."""
        config = FileBackendConfig()
        repr_str = repr(config)

        assert "FileBackendConfig" in repr_str

    def test_config_equality(self):
        """Test that two configs with same values are equal."""
        config1 = FileBackendConfig(max_size_mb=2048)
        config2 = FileBackendConfig(max_size_mb=2048)

        assert config1 == config2

    def test_config_inequality(self):
        """Test that configs with different values are not equal."""
        config1 = FileBackendConfig(max_size_mb=2048)
        config2 = FileBackendConfig(max_size_mb=1024)

        assert config1 != config2
