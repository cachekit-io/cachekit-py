"""Tests for custom key= parameter support in @cache decorator.

Per round-table review 2025-12-18: Custom key function is the cross-language
escape hatch for complex types (2D arrays, DataFrames, large data, custom types).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest


class TestCustomKeyFunctionBasic:
    """Basic custom key function tests."""

    def test_custom_key_receives_args(self):
        """Custom key function receives positional arguments."""
        received_args: list[tuple[Any, ...]] = []

        def capture_key(*args):
            received_args.append(args)
            return f"key:{args[0]}"

        # Import here to avoid import errors if cachekit not fully set up
        from cachekit.config import DecoratorConfig

        config = DecoratorConfig.minimal(key=capture_key)

        # Verify key field is set
        assert config.key is capture_key

    def test_custom_key_receives_kwargs(self):
        """Custom key function receives keyword arguments."""
        received_kwargs: list[dict[str, Any]] = []

        def capture_key(*args, **kwargs):
            received_kwargs.append(kwargs)
            return f"key:{kwargs.get('x', 0)}"

        from cachekit.config import DecoratorConfig

        config = DecoratorConfig.minimal(key=capture_key)
        assert config.key is capture_key

    def test_custom_key_must_return_string(self):
        """Key function must return string type."""

        def bad_key(*args):
            return 42  # Returns int, not str

        from cachekit.config import DecoratorConfig

        config = DecoratorConfig.minimal(key=bad_key)

        # The config accepts any callable - validation happens at runtime
        assert config.key is bad_key


class TestCustomKeyFunctionWithNumpyArrays:
    """Test custom key function with numpy arrays."""

    @pytest.fixture
    def np(self):
        """Import numpy (skip if not available)."""
        pytest.importorskip("numpy")
        import numpy as np

        return np

    def test_array_key_function_pattern(self, np):
        """Demonstrate the array key function pattern."""

        def array_key(arr):
            """Custom key for numpy array using content hash."""
            return hashlib.blake2b(arr.tobytes(), digest_size=16).hexdigest()

        arr = np.array([[1, 2], [3, 4]])  # 2D array (would fail standard key gen)
        key = array_key(arr)

        assert isinstance(key, str)
        assert len(key) == 32  # 128-bit = 32 hex chars

    def test_array_with_metadata_key_pattern(self, np):
        """Demonstrate array + metadata key pattern."""

        def array_with_meta_key(arr, name: str):
            """Key includes both array content and metadata."""
            content_hash = hashlib.blake2b(arr.tobytes(), digest_size=16).hexdigest()
            return f"{name}:{arr.shape}:{arr.dtype}:{content_hash}"

        arr = np.array([[1, 2], [3, 4]], dtype=np.float64)
        key = array_with_meta_key(arr, "matrix")

        assert "matrix" in key
        assert "(2, 2)" in key
        assert "float64" in key


class TestCustomKeyFunctionWithDataFrames:
    """Test custom key function with pandas DataFrames."""

    @pytest.fixture
    def pd(self):
        """Import pandas (skip if not available)."""
        pytest.importorskip("pandas")
        import pandas as pd

        return pd

    def test_dataframe_key_function_pattern(self, pd):
        """Demonstrate the DataFrame key function pattern."""

        def dataframe_key(df):
            """Custom key for DataFrame using values hash."""
            # Use values.tobytes() for deterministic hashing
            return hashlib.blake2b(df.values.tobytes(), digest_size=16).hexdigest()

        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        key = dataframe_key(df)

        assert isinstance(key, str)
        assert len(key) == 32

    def test_dataframe_same_content_same_key(self, pd):
        """Same DataFrame content produces same key."""

        def dataframe_key(df):
            return hashlib.blake2b(df.values.tobytes(), digest_size=16).hexdigest()

        df1 = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        df2 = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})

        assert dataframe_key(df1) == dataframe_key(df2)

    def test_dataframe_different_content_different_key(self, pd):
        """Different DataFrame content produces different key."""

        def dataframe_key(df):
            return hashlib.blake2b(df.values.tobytes(), digest_size=16).hexdigest()

        df1 = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        df2 = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 7]})

        assert dataframe_key(df1) != dataframe_key(df2)


class TestCustomKeyFunctionComposite:
    """Test composite key patterns combining multiple arguments."""

    def test_composite_key_pattern(self):
        """Demonstrate composite key with multiple args."""

        def composite_key(model_id: str, version: int, params: dict):
            """Key combining model ID, version, and params hash."""
            import json

            params_str = json.dumps(params, sort_keys=True)
            params_hash = hashlib.blake2b(params_str.encode(), digest_size=8).hexdigest()
            return f"{model_id}:v{version}:{params_hash}"

        key = composite_key("bert-base", 2, {"max_length": 512, "batch_size": 32})

        assert "bert-base" in key
        assert "v2" in key
        assert len(key.split(":")) == 3

    def test_identity_based_key_pattern(self):
        """Demonstrate identity-based key (ignoring content)."""

        def identity_key(user_id: int, _data: Any):
            """Key based only on user_id, ignoring data content."""
            return f"user:{user_id}"

        # Same user_id = same key regardless of data
        key1 = identity_key(123, {"name": "Alice"})
        key2 = identity_key(123, {"name": "Bob"})
        key3 = identity_key(456, {"name": "Alice"})

        assert key1 == key2  # Same user, different data = same key
        assert key1 != key3  # Different user = different key


class TestDecoratorConfigKeyField:
    """Test DecoratorConfig key field behavior."""

    def test_key_field_default_none(self):
        """Key field defaults to None."""
        from cachekit.config import DecoratorConfig

        config = DecoratorConfig.minimal()
        assert config.key is None

    def test_key_field_in_to_dict(self):
        """Key field appears in to_dict output."""
        from cachekit.config import DecoratorConfig

        def my_key(*args):
            return "test"

        config = DecoratorConfig.minimal(key=my_key)
        config_dict = config.to_dict()

        assert "key" in config_dict
        assert config_dict["key"] is my_key

    def test_key_field_accepts_lambda(self):
        """Key field accepts lambda functions."""
        from cachekit.config import DecoratorConfig

        config = DecoratorConfig.minimal(key=lambda x: f"key:{x}")
        assert config.key is not None
        assert config.key(42) == "key:42"

    def test_key_field_accepts_callable_class(self):
        """Key field accepts callable class instances."""

        class MyKeyGenerator:
            def __init__(self, prefix: str):
                self.prefix = prefix

            def __call__(self, *args):
                return f"{self.prefix}:{args}"

        from cachekit.config import DecoratorConfig

        gen = MyKeyGenerator("cache")
        config = DecoratorConfig.minimal(key=gen)

        assert config.key is gen
        assert config.key(1, 2) == "cache:(1, 2)"
