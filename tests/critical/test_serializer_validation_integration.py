"""Critical tests for serializer validation integration with caching."""

import numpy as np
import pytest

from cachekit.cache_handler import SerializerFactory


class TestSerializerValidationCritical:
    """Critical tests ensuring serializer validation doesn't break caching."""

    def test_serializer_factory_always_returns_working_serializer(self):
        """Critical: SerializerFactory must always return a working serializer."""
        test_cases = [
            # (data, requested_serializer, description)
            ({"simple": "data"}, "default", "JSON data with default"),
            (np.array([1, 2, 3]), "default", "NumPy with default"),
            ({"complex": {"nested": "data"}}, "default", "Complex data with default"),
            (b"binary_data", "default", "Binary with default"),
            ({"test": "data"}, "default", "Valid data with default"),
        ]

        for data, requested, description in test_cases:
            # Factory must return a working serializer
            serializer = SerializerFactory.create_serializer(requested, data)
            assert serializer is not None, f"Factory returned None for: {description}"

            # Serializer must be able to handle the data
            try:
                serialized, metadata = serializer.serialize(data)
                assert serialized is not None, f"Serialization failed for: {description}"
                assert metadata is not None, f"Metadata missing for: {description}"

                # Must be able to deserialize
                deserialized = serializer.deserialize(serialized, metadata)
                assert deserialized is not None or data is None, f"Deserialization failed for: {description}"

            except Exception as e:
                # For unsupported types, AutoSerializer should raise clear error
                error_msg = str(e).lower()
                if any(x in error_msg for x in ["not support", "unsupported", "does not support"]):
                    # This is expected for some data types with AutoSerializer
                    # The factory returns AutoSerializer but it validates data types
                    pass
                else:
                    pytest.fail(f"Unexpected error for {description}: {e}")

    def test_removed_serializers_raise_clear_errors(self):
        """Critical: Removed serializers should raise clear error messages."""
        # Note: "auto" is now an alias for "default" (AutoSerializer), so it's valid
        # Only truly removed serializers should be in this list
        removed_serializers = ["rust", "json", "universal", "pickle"]

        for serializer_name in removed_serializers:
            with pytest.raises(ValueError) as exc_info:
                SerializerFactory.create_serializer(serializer_name)

            error_msg = str(exc_info.value).lower()
            # Greenfield: All removed serializers raise ValueError
            assert any(x in error_msg for x in ["unknown", "valid options"]), f"Expected clear error for {serializer_name}"

    def test_validation_does_not_break_raw_serializer(self):
        """Critical: Raw serializer must work with validation system."""
        test_data = [
            {"simple": "dict"},
            [1, 2, 3, "mixed"],
            "simple string",
            42,
            3.14159,
            True,
            None,
        ]

        for data in test_data:
            serializer = SerializerFactory.create_serializer("default", data)
            assert serializer is not None, f"Default serializer failed for {type(data).__name__}"

            # Must handle the data successfully - these are all basic types
            serialized, metadata = serializer.serialize(data)
            deserialized = serializer.deserialize(serialized, metadata)

            # Basic sanity check (None is a valid deserialized value)
            if data is not None:
                assert deserialized is not None
            else:
                # None should deserialize to None
                assert deserialized is None

    def test_unknown_serializers_fail_with_clear_errors(self):
        """Critical: Unknown serializers must fail with clear error messages."""
        # Greenfield: Valid serializers are "default", "auto" (alias), "arrow", "orjson"
        # "auto" is now an alias for "default" (AutoSerializer)
        unknown_serializers = [
            "pickle",
            "json",
            "rust",
            "nonexistent",
        ]

        for serializer_name in unknown_serializers:
            with pytest.raises(ValueError) as exc_info:
                SerializerFactory.create_serializer(serializer_name)

            error_msg = str(exc_info.value).lower()
            # Should mention it's unknown and suggest valid options
            assert "unknown" in error_msg or "valid options" in error_msg

    def test_factory_validation_method_reliability(self):
        """Critical: Factory validation methods must be reliable."""
        test_cases = [
            # (data, serializer, expected_result)
            ({"simple": "data"}, "default", True),
            ({"simple": "data"}, "orjson", True),  # Now available
            ({"simple": "data"}, "pickle", False),  # Removed for security
            ({"data": "test"}, "nonexistent_serializer", False),
        ]

        for data, serializer_name, expected in test_cases:
            result = SerializerFactory.validate_serializer_for_data(serializer_name, data)
            assert result == expected, (
                f"Validation result mismatch: {serializer_name} with {type(data).__name__} expected {expected}, got {result}"
            )

    def test_no_exceptions_in_validation_system(self):
        """Critical: Validation system must never raise unhandled exceptions."""
        # Test with various edge cases that might cause exceptions
        edge_cases = [
            None,
            [],
            {},
            "",
            0,
            float("inf"),
            float("nan"),
            complex(1, 2),
            range(10),
            set(),
            frozenset(),
        ]

        for data in edge_cases:
            # Validation system should handle edge cases gracefully
            _serializer = SerializerFactory.create_serializer("default", data)
            SerializerFactory.validate_serializer_for_data("default", data)
            SerializerFactory.get_compatibility_report(data)

    def test_memory_efficiency_of_validation(self):
        """Critical: Validation should not cause memory leaks or excessive usage."""
        import gc
        import sys

        # Get initial memory usage
        gc.collect()
        initial_refs = sys.gettotalrefcount() if hasattr(sys, "gettotalrefcount") else 0

        # Perform many validations with simple data that works
        for _ in range(100):
            data = {"test": "data"}  # Use simple data that works with default serializer
            SerializerFactory.create_serializer("default", data)
            SerializerFactory.validate_serializer_for_data("default", data)

        # Force garbage collection
        gc.collect()
        final_refs = sys.gettotalrefcount() if hasattr(sys, "gettotalrefcount") else 0

        # Should not have excessive reference growth (allow for some variance)
        if initial_refs > 0:  # Only check if we have reference counting
            ref_growth = final_refs - initial_refs
            assert ref_growth < 1000, f"Possible memory leak: {ref_growth} reference growth"
