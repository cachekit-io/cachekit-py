"""Unit tests for InvalidationEvent dataclass.

Tests cover serialization, validation, and security edge cases for the
InvalidationEvent class used in cross-pod L1 cache invalidation.

Test marks:
    @pytest.mark.unit: All tests are unit-scoped (no Redis, no async)
    @pytest.mark.security: Security-critical edge case tests
"""

import msgpack
import pytest

from cachekit.invalidation.event import (
    MAX_MESSAGE_SIZE,
    InvalidationEvent,
    InvalidationLevel,
)


class TestSerialization:
    """Serialization roundtrip tests for all invalidation levels."""

    @pytest.mark.unit
    def test_global_event_roundtrip(self) -> None:
        """Test GLOBAL level event serializes and deserializes correctly."""
        event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)

        assert restored.level == InvalidationLevel.GLOBAL
        assert restored.namespace is None
        assert restored.params_hash is None
        assert restored == event

    @pytest.mark.unit
    def test_namespace_event_roundtrip(self) -> None:
        """Test NAMESPACE level event serializes and deserializes correctly."""
        event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="user_cache",
            params_hash=None,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)

        assert restored.level == InvalidationLevel.NAMESPACE
        assert restored.namespace == "user_cache"
        assert restored.params_hash is None
        assert restored == event

    @pytest.mark.unit
    def test_params_event_roundtrip(self) -> None:
        """Test PARAMS level event serializes and deserializes correctly."""
        params_hash = "a" * 64  # Valid 64-char lowercase hex
        event = InvalidationEvent(
            level=InvalidationLevel.PARAMS,
            namespace=None,
            params_hash=params_hash,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)

        assert restored.level == InvalidationLevel.PARAMS
        assert restored.namespace is None
        assert restored.params_hash == params_hash
        assert restored == event

    @pytest.mark.unit
    def test_compact_serialization(self) -> None:
        """Verify compact key names in MessagePack output."""
        event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="test_ns",
            params_hash=None,
        )
        data = event.to_bytes()

        # Deserialize raw to inspect keys
        payload = msgpack.unpackb(data, raw=False)

        # Verify compact key names
        assert "l" in payload
        assert "ns" in payload
        assert payload["l"] == "namespace"
        assert payload["ns"] == "test_ns"
        # "ph" should not be present when params_hash is None
        assert "ph" not in payload


class TestValidation:
    """Validation tests for InvalidationEvent constraints."""

    @pytest.mark.unit
    def test_namespace_level_requires_namespace(self) -> None:
        """NAMESPACE level must have namespace field."""
        with pytest.raises(ValueError, match="NAMESPACE level requires namespace"):
            InvalidationEvent(
                level=InvalidationLevel.NAMESPACE,
                namespace=None,
                params_hash=None,
            )

    @pytest.mark.unit
    def test_params_level_requires_params_hash(self) -> None:
        """PARAMS level must have params_hash field."""
        with pytest.raises(ValueError, match="PARAMS level requires params_hash"):
            InvalidationEvent(
                level=InvalidationLevel.PARAMS,
                namespace=None,
                params_hash=None,
            )

    @pytest.mark.unit
    def test_params_hash_must_be_64_hex_exact_length(self) -> None:
        """params_hash must be exactly 64 characters."""
        # Test 63 characters (too short)
        with pytest.raises(ValueError, match="64-character lowercase hex"):
            InvalidationEvent(
                level=InvalidationLevel.PARAMS,
                namespace=None,
                params_hash="a" * 63,
            )

        # Test 65 characters (too long)
        with pytest.raises(ValueError, match="64-character lowercase hex"):
            InvalidationEvent(
                level=InvalidationLevel.PARAMS,
                namespace=None,
                params_hash="a" * 65,
            )

    @pytest.mark.unit
    def test_params_hash_must_be_lowercase_hex(self) -> None:
        """params_hash must be lowercase hex, reject uppercase."""
        # Uppercase hex should be rejected
        with pytest.raises(ValueError, match="64-character lowercase hex"):
            InvalidationEvent(
                level=InvalidationLevel.PARAMS,
                namespace=None,
                params_hash="A" * 64,
            )

    @pytest.mark.unit
    def test_params_hash_rejects_non_hex_chars(self) -> None:
        """params_hash must contain only hex characters."""
        # Non-hex characters
        invalid_hashes = [
            "g" * 64,  # 'g' is not hex
            "z" * 64,  # 'z' is not hex
            "a" * 63 + "x",  # Last char not hex
            "a" * 63 + " ",  # Space not hex
        ]

        for invalid_hash in invalid_hashes:
            with pytest.raises(ValueError, match="64-character lowercase hex"):
                InvalidationEvent(
                    level=InvalidationLevel.PARAMS,
                    namespace=None,
                    params_hash=invalid_hash,
                )

    @pytest.mark.unit
    def test_invalid_namespace_format_rejected(self) -> None:
        """Namespace must match pattern: alphanumeric, underscore, hyphen, 1-128 chars."""
        invalid_namespaces = [
            "ns with spaces",  # Spaces not allowed
            "ns@special",  # Special chars not allowed
            "ns!",  # ! not allowed
            "ns.dot",  # . not allowed
            "",  # Empty not allowed
            "a" * 129,  # Too long (>128 chars)
            "ns~",  # Tilde not allowed
            "ns#hash",  # Hash not allowed
        ]

        for invalid_ns in invalid_namespaces:
            with pytest.raises(ValueError, match="namespace must match pattern"):
                InvalidationEvent(
                    level=InvalidationLevel.NAMESPACE,
                    namespace=invalid_ns,
                    params_hash=None,
                )

    @pytest.mark.unit
    def test_valid_namespace_formats(self) -> None:
        """Valid namespaces should be accepted."""
        valid_namespaces = [
            "a",  # Single char
            "a_b",  # Underscore
            "a-b",  # Hyphen
            "abc123",  # Alphanumeric
            "user_cache",  # Realistic
            "A_Z",  # Uppercase allowed
            "a" * 128,  # Max length (128 chars)
        ]

        for valid_ns in valid_namespaces:
            event = InvalidationEvent(
                level=InvalidationLevel.NAMESPACE,
                namespace=valid_ns,
                params_hash=None,
            )
            assert event.namespace == valid_ns

    @pytest.mark.unit
    def test_global_level_rejects_namespace(self) -> None:
        """GLOBAL level must have namespace=None."""
        with pytest.raises(ValueError, match="GLOBAL level must have"):
            InvalidationEvent(
                level=InvalidationLevel.GLOBAL,
                namespace="something",
                params_hash=None,
            )

    @pytest.mark.unit
    def test_global_level_rejects_params_hash(self) -> None:
        """GLOBAL level must have params_hash=None."""
        with pytest.raises(ValueError, match="GLOBAL level must have"):
            InvalidationEvent(
                level=InvalidationLevel.GLOBAL,
                namespace=None,
                params_hash="a" * 64,
            )


class TestSecurityEdgeCases:
    """Security-critical edge case tests."""

    @pytest.mark.unit
    @pytest.mark.security
    def test_oversized_payload_rejected(self) -> None:
        """Reject payloads larger than MAX_MESSAGE_SIZE (10KB)."""
        # Craft a payload that exceeds 10KB when packed
        huge_payload = msgpack.packb({"l": "global", "data": "x" * (MAX_MESSAGE_SIZE + 1)})

        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(huge_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_max_string_length_enforced(self) -> None:
        """Reject strings longer than 1024 characters."""
        # Create a namespace at the limit (should work)
        event_ok = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="a" * 128,  # Max valid namespace size (128 chars)
            params_hash=None,
        )
        data_ok = event_ok.to_bytes()
        assert InvalidationEvent.from_bytes(data_ok) == event_ok

        # Try to deserialize a payload with a string >1024 chars
        # This requires crafting raw msgpack since InvalidationEvent
        # validates namespace at construction
        huge_string_payload = msgpack.packb({"l": "namespace", "ns": "x" * 1025})

        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(huge_string_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_max_array_length_enforced(self) -> None:
        """Reject arrays with >100 elements."""
        # Craft payload with an array >100 elements
        oversized_array_payload = msgpack.packb({"l": "global", "data": list(range(101))})

        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(oversized_array_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_max_map_length_enforced(self) -> None:
        """Reject maps with >100 keys."""
        # Craft payload with >100 keys
        oversized_map = {f"key_{i}": f"value_{i}" for i in range(101)}
        oversized_map["l"] = "global"
        oversized_map_payload = msgpack.packb(oversized_map)

        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(oversized_map_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_malformed_msgpack_rejected(self) -> None:
        """Reject truncated or malformed MessagePack."""
        # Valid MessagePack starts with 0x81 (1-element map) but truncate it
        truncated_msgpack = b"\x81\xa1l"  # Incomplete

        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(truncated_msgpack)

    @pytest.mark.unit
    @pytest.mark.security
    def test_missing_required_keys(self) -> None:
        """Reject payloads missing required 'l' (level) key."""
        # Valid: contains all required fields
        valid_payload = msgpack.packb({"l": "global"})
        event = InvalidationEvent.from_bytes(valid_payload)
        assert event.level == InvalidationLevel.GLOBAL

        # Invalid: missing 'l' key
        missing_level = msgpack.packb({"ns": "test"})
        with pytest.raises(ValueError, match="Missing required field 'l'"):
            InvalidationEvent.from_bytes(missing_level)

    @pytest.mark.unit
    @pytest.mark.security
    def test_invalid_level_value_rejected(self) -> None:
        """Reject invalid level values."""
        invalid_level_payload = msgpack.packb({"l": "invalid_level"})

        with pytest.raises(ValueError, match="Invalid level value"):
            InvalidationEvent.from_bytes(invalid_level_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_non_dict_payload_rejected(self) -> None:
        """Reject payloads that are not dictionaries."""
        # List instead of dict
        list_payload = msgpack.packb(["global"])

        with pytest.raises(ValueError, match="Invalid payload: expected dict"):
            InvalidationEvent.from_bytes(list_payload)

        # String instead of dict
        string_payload = msgpack.packb("global")

        with pytest.raises(ValueError, match="Invalid payload: expected dict"):
            InvalidationEvent.from_bytes(string_payload)

    @pytest.mark.unit
    @pytest.mark.security
    def test_empty_bytes_rejected(self) -> None:
        """Reject empty byte payloads."""
        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(b"")

    @pytest.mark.unit
    @pytest.mark.security
    def test_binary_data_rejected(self) -> None:
        """Reject random binary data."""
        with pytest.raises(ValueError, match="Failed to deserialize"):
            InvalidationEvent.from_bytes(b"\x00\x01\x02\x03")


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.unit
    def test_namespace_boundary_min_length(self) -> None:
        """Test namespace at minimum length (1 char)."""
        event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="a",
            params_hash=None,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)
        assert restored.namespace == "a"

    @pytest.mark.unit
    def test_namespace_boundary_max_length(self) -> None:
        """Test namespace at maximum length (128 chars)."""
        max_ns = "a" * 128
        event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace=max_ns,
            params_hash=None,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)
        assert restored.namespace == max_ns

    @pytest.mark.unit
    def test_params_hash_all_hex_chars(self) -> None:
        """Test params_hash with all valid hex chars (0-9, a-f)."""
        # Use all hex chars
        all_hex = "0123456789abcdef" * 4  # 64 chars total
        event = InvalidationEvent(
            level=InvalidationLevel.PARAMS,
            namespace=None,
            params_hash=all_hex,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)
        assert restored.params_hash == all_hex

    @pytest.mark.unit
    def test_namespace_with_numbers_and_special_chars(self) -> None:
        """Test namespace with allowed special characters."""
        valid_ns = "user_123-cache_v2"
        event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace=valid_ns,
            params_hash=None,
        )
        data = event.to_bytes()
        restored = InvalidationEvent.from_bytes(data)
        assert restored.namespace == valid_ns

    @pytest.mark.unit
    def test_serialization_size_efficiency(self) -> None:
        """Verify serialization is reasonably compact."""
        # GLOBAL event should be very small
        global_event = InvalidationEvent(
            level=InvalidationLevel.GLOBAL,
            namespace=None,
            params_hash=None,
        )
        global_data = global_event.to_bytes()
        assert len(global_data) < 50  # Should be tiny

        # NAMESPACE event should be <200 bytes for reasonable namespace
        ns_event = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="user_cache",
            params_hash=None,
        )
        ns_data = ns_event.to_bytes()
        assert len(ns_data) < 200

        # PARAMS event should be <100 bytes
        params_event = InvalidationEvent(
            level=InvalidationLevel.PARAMS,
            namespace=None,
            params_hash="a" * 64,
        )
        params_data = params_event.to_bytes()
        assert len(params_data) < 100

    @pytest.mark.unit
    def test_immutability(self) -> None:
        """Verify InvalidationEvent is immutable (frozen dataclass)."""
        event = InvalidationEvent(
            level=InvalidationLevel.GLOBAL,
            namespace=None,
            params_hash=None,
        )

        # Attempt to modify should raise FrozenInstanceError
        with pytest.raises((AttributeError, TypeError)):
            event.level = InvalidationLevel.NAMESPACE  # type: ignore

    @pytest.mark.unit
    def test_equality_comparison(self) -> None:
        """Test equality comparison between events."""
        event1 = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="cache",
            params_hash=None,
        )
        event2 = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="cache",
            params_hash=None,
        )
        event3 = InvalidationEvent(
            level=InvalidationLevel.NAMESPACE,
            namespace="different",
            params_hash=None,
        )

        assert event1 == event2
        assert event1 != event3
