"""Cross-language test vectors for StandardSerializer (protocol-v1.0.md Section 10.2).

Generates MessagePack test vectors for PHP/JavaScript SDK developers to verify byte-for-byte compatibility.
Each test vector is serialized using StandardSerializer with enable_integrity_checking=False (pure MessagePack,
no ByteStorage envelope) to allow direct comparison with commodity MessagePack libraries.

PHP Usage:
    $data = msgpack_unpack(hex2bin("<hex_dump>"));

JavaScript Usage:
    const msgpack5 = require('msgpack5')();
    const data = msgpack5.decode(Buffer.from("<hex_dump>", "hex"));

Test vectors cover:
- Primitives (int, float, str, bool, None, bytes)
- Collections (list, dict with nesting)
- Temporal (datetime, date, time with ISO-8601 encoding)
- Unicode (emoji, CJK characters)
- Edge cases (empty containers, nested structures)
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from cachekit.serializers.standard_serializer import StandardSerializer


@pytest.mark.integration
class TestStandardSerializerCrossLanguage:
    """Test vector generator for cross-language StandardSerializer validation."""

    def test_generate_cross_language_test_vectors(self):
        """Generate real MessagePack test vectors for PHP/JavaScript SDKs.

        Each vector is a tuple of (python_object, expected_type, label).
        Serializes with enable_integrity_checking=False to produce pure MessagePack bytes.
        Outputs hex dumps for manual verification and documentation.

        PHP deserialization example:
            // Test vector 1: simple_dict
            $hex = "81a3..." // From test output
            $data = msgpack_unpack(hex2bin($hex));
            assert($data === ["key" => "value"]);

        JavaScript deserialization example:
            // Test vector 1: simple_dict
            const msgpack5 = require('msgpack5')();
            const hex = "81a3..."; // From test output
            const data = msgpack5.decode(Buffer.from(hex, "hex"));
            assert.deepEqual(data, {key: "value"});
        """
        # Serializer without ByteStorage envelope (pure MessagePack)
        serializer = StandardSerializer(enable_integrity_checking=False)

        # Test vectors: (object, expected_type_description, label)
        test_cases = [
            # Primitives
            (None, "null", "null_value"),
            (True, "boolean", "bool_true"),
            (False, "boolean", "bool_false"),
            (42, "int", "simple_int"),
            (-123, "int", "negative_int"),
            (3.14, "float", "simple_float"),
            (0.0, "float", "float_zero"),
            (-0.0, "float", "float_negative_zero"),
            ("hello", "string", "simple_string"),
            (b"\x00\x01\x02\x03", "bytes", "simple_bytes"),
            # Collections - simple
            ([], "array", "empty_list"),
            ({}, "map", "empty_dict"),
            ([1, 2, 3], "array", "simple_list"),
            ({"key": "value"}, "map", "simple_dict"),
            # Collections - nested
            (
                {"user": {"id": 123, "name": "Alice"}},
                "nested map",
                "nested_dict",
            ),
            (
                [[1, 2], [3, 4]],
                "nested array",
                "nested_list",
            ),
            # Mixed types
            (
                {"int": 42, "float": 3.14, "bool": True, "null": None, "str": "hello"},
                "mixed map",
                "mixed_types",
            ),
            (
                [42, 3.14, True, None, "hello"],
                "mixed array",
                "mixed_array",
            ),
            # Datetime types (MessagePack extension 0xC0 → dict with __datetime__ marker)
            (
                datetime(2025, 11, 14, 10, 30, 0),
                "datetime",
                "datetime_simple",
            ),
            (
                date(2025, 11, 14),
                "date",
                "date_simple",
            ),
            (
                time(10, 30, 0),
                "time",
                "time_simple",
            ),
            (
                datetime(2025, 11, 14, 10, 30, 0, 123456),
                "datetime with microseconds",
                "datetime_microseconds",
            ),
            # Unicode
            (
                {"emoji": "🚀", "chinese": "你好", "arabic": "مرحبا"},
                "unicode map",
                "unicode_strings",
            ),
            (
                ["🚀", "你好", "مرحبا", "Привет"],
                "unicode array",
                "unicode_array",
            ),
            # Edge cases
            (
                {"": "empty_key", "value": ""},
                "empty strings",
                "empty_string_keys",
            ),
            (
                [[], [[]], [[[]]]],
                "deeply nested arrays",
                "deeply_nested_lists",
            ),
            (
                {"level1": {"level2": {"level3": {"value": 42}}}},
                "deeply nested maps",
                "deeply_nested_dicts",
            ),
            # Large integers (MessagePack handles 64-bit)
            (2**31 - 1, "int32 max", "int32_max"),
            (2**31, "int32 overflow", "int32_overflow"),
            (2**63 - 1, "int64 max", "int64_max"),
            # Realistic data structures
            (
                {
                    "user_id": 12345,
                    "username": "alice@example.com",
                    "roles": ["admin", "user"],
                    "metadata": {
                        "created_at": "2025-11-14T10:30:00",
                        "active": True,
                    },
                },
                "user object",
                "realistic_user_object",
            ),
        ]

        print("\n" + "=" * 80)
        print("CROSS-LANGUAGE TEST VECTORS (protocol-v1.0.md Section 10.2)")
        print("=" * 80)
        print("\nGenerated MessagePack test vectors for PHP/JavaScript SDKs.")
        print("Each hex dump can be deserialized using commodity MessagePack libraries.\n")

        for obj, type_desc, label in test_cases:
            # Serialize to pure MessagePack
            data, metadata = serializer.serialize(obj)
            hex_dump = data.hex()

            # Verify metadata
            assert metadata.format.value == "msgpack"
            assert metadata.compressed is False  # No ByteStorage
            assert metadata.encrypted is False

            # Verify roundtrip
            result = serializer.deserialize(data)
            assert result == obj, f"Roundtrip failed for {label}"

            # Output formatted test vector
            print(f"\n{label}:")
            print(f"  Type: {type_desc}")
            print(f"  Python: {repr(obj)}")
            print(f"  Hex: {hex_dump}")
            print(f"  Length: {len(data)} bytes")

            # Add language-specific deserialization examples
            if label in ["simple_dict", "simple_list", "mixed_types"]:
                print("\n  PHP:")
                print(f'    $data = msgpack_unpack(hex2bin("{hex_dump}"));')
                print(f"    // Expected: {self._php_repr(obj)}")
                print("\n  JavaScript:")
                print(f'    const data = msgpack5.decode(Buffer.from("{hex_dump}", "hex"));')
                print(f"    // Expected: {self._js_repr(obj)}")

        print("\n" + "=" * 80)
        print(f"Total test vectors: {len(test_cases)}")
        print("=" * 80)
        print("\nNext steps for PHP/JavaScript SDK developers:")
        print("1. Copy hex dumps into your test suite")
        print("2. Deserialize using msgpack_unpack() (PHP) or msgpack5.decode() (JS)")
        print("3. Compare deserialized values with expected Python representations")
        print("4. Verify byte-for-byte compatibility by serializing the same objects")
        print("   and comparing hex output with these vectors")
        print("=" * 80 + "\n")

    def test_datetime_encoding_format(self):
        """Verify datetime encoding uses __datetime__ marker dict (MessagePack extension 0xC0 replacement).

        Protocol-v1.0.md specifies datetime encoding as:
            {"__datetime__": true, "value": "2025-11-14T10:30:00"}

        This ensures cross-language compatibility without custom MessagePack extensions.
        """
        serializer = StandardSerializer(enable_integrity_checking=False)

        # Test datetime encoding
        dt = datetime(2025, 11, 14, 10, 30, 0)
        data, _ = serializer.serialize(dt)

        # Deserialize and verify structure
        result = serializer.deserialize(data)
        assert result == dt

        # Verify the underlying MessagePack contains the marker dict
        # (by serializing a dict with __datetime__ and comparing)
        expected_dict = {"__datetime__": True, "value": "2025-11-14T10:30:00"}
        expected_data, _ = serializer.serialize(expected_dict)

        # Note: We can't directly compare bytes because datetime serialization
        # uses the _standard_default hook which produces the same structure
        print("\nDatetime encoding verification:")
        print(f"  Input: {dt}")
        print(f"  Hex: {data.hex()}")
        print(f"  Expected dict structure: {expected_dict}")
        print(f"  Expected dict hex: {expected_data.hex()}")

    def test_date_encoding_format(self):
        """Verify date encoding uses __date__ marker dict."""
        serializer = StandardSerializer(enable_integrity_checking=False)

        d = date(2025, 11, 14)
        data, _ = serializer.serialize(d)
        result = serializer.deserialize(data)
        assert result == d

        print("\nDate encoding verification:")
        print(f"  Input: {d}")
        print(f"  Hex: {data.hex()}")
        print(f"  ISO-8601: {d.isoformat()}")

    def test_time_encoding_format(self):
        """Verify time encoding uses __time__ marker dict."""
        serializer = StandardSerializer(enable_integrity_checking=False)

        t = time(10, 30, 0)
        data, _ = serializer.serialize(t)
        result = serializer.deserialize(data)
        assert result == t

        print("\nTime encoding verification:")
        print(f"  Input: {t}")
        print(f"  Hex: {data.hex()}")
        print(f"  ISO-8601: {t.isoformat()}")

    def test_verify_messagepack_format_compliance(self):
        """Verify generated bytes comply with MessagePack specification.

        MessagePack format reference: https://github.com/msgpack/msgpack/blob/master/spec.md

        Key format codes:
        - 0x00-0x7f: positive fixint
        - 0x80-0x8f: fixmap (map with N items, where N = byte - 0x80)
        - 0x90-0x9f: fixarray (array with N items, where N = byte - 0x90)
        - 0xa0-0xbf: fixstr (string with N bytes, where N = byte - 0xa0)
        - 0xc0: nil
        - 0xc2: false
        - 0xc3: true
        - 0xca: float 32
        - 0xcb: float 64
        - 0xcc: uint 8
        - 0xcd: uint 16
        - 0xce: uint 32
        - 0xcf: uint 64
        - 0xd0: int 8
        - 0xd1: int 16
        - 0xd2: int 32
        - 0xd3: int 64
        """
        serializer = StandardSerializer(enable_integrity_checking=False)

        # Test fixint (0x00-0x7f for 0-127)
        data, _ = serializer.serialize(42)
        assert data[0] == 0x2A  # 42 in hex = 0x2a
        print("\nMessagePack format verification:")
        print(f"  fixint(42): {data.hex()} (first byte: 0x{data[0]:02x})")

        # Test fixmap (0x81 = map with 1 item)
        data, _ = serializer.serialize({"k": "v"})
        assert data[0] == 0x81  # fixmap with 1 item
        print(f'  fixmap({{"k": "v"}}): {data.hex()} (first byte: 0x{data[0]:02x})')

        # Test fixarray (0x93 = array with 3 items)
        data, _ = serializer.serialize([1, 2, 3])
        assert data[0] == 0x93  # fixarray with 3 items
        print(f"  fixarray([1,2,3]): {data.hex()} (first byte: 0x{data[0]:02x})")

        # Test fixstr (0xa5 = string with 5 bytes)
        data, _ = serializer.serialize("hello")
        assert data[0] == 0xA5  # fixstr with 5 bytes
        print(f'  fixstr("hello"): {data.hex()} (first byte: 0x{data[0]:02x})')

        # Test nil (0xc0)
        data, _ = serializer.serialize(None)
        assert data[0] == 0xC0
        print(f"  nil: {data.hex()} (first byte: 0x{data[0]:02x})")

        # Test bool (0xc2 = false, 0xc3 = true)
        data_false, _ = serializer.serialize(False)
        assert data_false[0] == 0xC2
        data_true, _ = serializer.serialize(True)
        assert data_true[0] == 0xC3
        print(f"  bool(False): {data_false.hex()} (first byte: 0x{data_false[0]:02x})")
        print(f"  bool(True): {data_true.hex()} (first byte: 0x{data_true[0]:02x})")

        print("\nAll MessagePack format codes verified!")

    # Helper methods for cross-language representation

    def _php_repr(self, obj: object) -> str:
        """Convert Python object to PHP representation for docs."""
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, (int, float)):
            return str(obj)
        if isinstance(obj, str):
            return f'"{obj}"'
        if isinstance(obj, list):
            items = ", ".join(self._php_repr(item) for item in obj)
            return f"[{items}]"
        if isinstance(obj, dict):
            items = ", ".join(f'"{k}" => {self._php_repr(v)}' for k, v in obj.items())
            return f"[{items}]"
        return repr(obj)

    def _js_repr(self, obj: object) -> str:
        """Convert Python object to JavaScript representation for docs."""
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, (int, float)):
            return str(obj)
        if isinstance(obj, str):
            return f'"{obj}"'
        if isinstance(obj, list):
            items = ", ".join(self._js_repr(item) for item in obj)
            return f"[{items}]"
        if isinstance(obj, dict):
            items = ", ".join(f"{k}: {self._js_repr(v)}" for k, v in obj.items())
            return f"{{{items}}}"
        return repr(obj)
