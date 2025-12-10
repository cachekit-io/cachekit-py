"""Test AutoSerializer type fidelity to prevent regressions."""

import pytest

from cachekit.serializers import AutoSerializer


class TestAutoSerializerTypeFidelity:
    """Comprehensive tests for AutoSerializer type preservation."""

    @pytest.fixture
    def serializer(self):
        """Create AutoSerializer instance."""
        return AutoSerializer()

    def test_nested_dict_with_booleans(self, serializer):
        """Test that nested dicts with booleans maintain type fidelity."""
        test_data = {
            "level1": {
                "level2a": {
                    "values": [1, 2, 3],
                    "flags": [True, False, True],  # Booleans that must be preserved
                    "name": "test",
                },
                "level2b": {
                    "count": 42,
                    "active": True,  # Another boolean
                    "items": ["a", "b", "c"],
                },
            },
            "metadata": {
                "version": 1.0,
                "enabled": False,  # Yet another boolean
            },
        }

        # Serialize and deserialize
        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        # Check exact equality
        assert test_data == deserialized

        # Check specific boolean values
        assert deserialized["level1"]["level2a"]["flags"] == [True, False, True]
        assert deserialized["level1"]["level2b"]["active"] is True
        assert deserialized["metadata"]["enabled"] is False

        # Ensure no type map leakage
        deserialized_str = str(deserialized)
        assert "__rust_type_map__" not in deserialized_str
        assert "b'__rust_type_map__'" not in deserialized_str

    def test_dict_with_byte_data_key(self, serializer):
        """Test that dicts containing 'data' as a key don't break unwrapping."""
        test_data = {
            "data": {"nested": "value"},  # This used to break the unwrapping logic
            "other": "value",
            "flags": [True, False],
        }

        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert test_data == deserialized
        assert "data" in deserialized
        assert deserialized["data"] == {"nested": "value"}

    def test_complex_nested_structure(self, serializer):
        """Test complex nested structures with mixed types."""
        test_data = {
            "users": [
                {
                    "id": 1,
                    "name": "Alice",
                    "active": True,
                    "scores": [95.5, 87.3, 92.1],
                    "metadata": {"created": "2024-01-01", "verified": False, "tags": ["admin", "user"]},
                },
                {
                    "id": 2,
                    "name": "Bob",
                    "active": False,
                    "scores": None,
                    "metadata": {"created": "2024-01-02", "verified": True, "tags": []},
                },
            ],
            "settings": {"debug": False, "max_users": 1000, "features": {"chat": True, "video": False, "analytics": True}},
        }

        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert test_data == deserialized

        # Verify specific nested boolean values
        assert deserialized["users"][0]["active"] is True
        assert deserialized["users"][0]["metadata"]["verified"] is False
        assert deserialized["users"][1]["active"] is False
        assert deserialized["users"][1]["metadata"]["verified"] is True
        assert deserialized["settings"]["debug"] is False
        assert deserialized["settings"]["features"]["chat"] is True
        assert deserialized["settings"]["features"]["video"] is False

    def test_all_basic_types(self, serializer):
        """Test all basic Python types maintain fidelity."""
        test_data = {
            "none": None,
            "bool_true": True,
            "bool_false": False,
            "int": 42,
            "float": 3.14159,
            "string": "hello world",
            "list": [1, 2, 3],
            "dict": {"nested": "value"},
            "mixed_list": [True, 42, "string", None, 3.14],
            "empty_list": [],
            "empty_dict": {},
        }

        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert test_data == deserialized

        # Check specific type preservation
        assert deserialized["none"] is None
        assert deserialized["bool_true"] is True
        assert deserialized["bool_false"] is False
        assert isinstance(deserialized["int"], int)
        assert isinstance(deserialized["float"], float)
        assert isinstance(deserialized["string"], str)

    def test_no_metadata_pollution(self, serializer):
        """Ensure internal metadata doesn't pollute results."""
        test_data = {
            "__rust_type_map__": "user_value",  # User happens to use this key
            "data": "another_value",
            "normal": {"key": True},
        }

        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        # User's keys should be preserved exactly
        assert test_data == deserialized
        assert deserialized["__rust_type_map__"] == "user_value"
        assert deserialized["data"] == "another_value"

    @pytest.mark.parametrize("depth", [1, 5, 10])
    def test_deeply_nested_dicts(self, serializer, depth):
        """Test deeply nested dictionary structures."""
        # Build nested structure
        data = {"level0": True}
        current = data
        for i in range(1, depth + 1):
            current[f"level{i}"] = {
                f"value{i}": i,
                f"flag{i}": i % 2 == 0,  # Alternating booleans
                f"data{i}": f"test{i}",  # Include 'data' in keys
            }
            current = current[f"level{i}"]

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert data == deserialized

        # Verify boolean preservation at each level
        current = deserialized
        for i in range(1, depth + 1):
            current = current[f"level{i}"]
            expected_bool = i % 2 == 0
            assert current[f"flag{i}"] is expected_bool

    def test_performance_dict_nested_pattern(self, serializer):
        """Test the exact pattern used in dict_nested benchmarks."""

        # Simulate dict_nested benchmark data structure
        def create_nested_dict(levels, keys_per_level=3):
            if levels == 0:
                return {"value": 42, "flag": True, "items": [1, 2, 3]}

            result = {}
            for i in range(keys_per_level):
                result[f"key_{i}"] = create_nested_dict(levels - 1, keys_per_level)
            result["metadata"] = {"level": levels, "active": levels % 2 == 0}
            return result

        test_data = create_nested_dict(3)  # 3 levels deep

        serialized, metadata = serializer.serialize(test_data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert test_data == deserialized

        # No type map should be exposed
        assert "__rust_type_map__" not in str(deserialized)
        assert "b'__rust_type_map__'" not in str(deserialized)
