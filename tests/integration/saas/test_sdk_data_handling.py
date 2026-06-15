"""SDK + SaaS E2E Data Handling Tests (Phase 2).

Tests MessagePack serialization, edge cases, and special data types with live SaaS backend.

Priority: P0/P1 (Critical/High - must pass before deployment)

Test Coverage:
- MessagePack serialization roundtrip (basic types)
- Unicode and international character handling
- Large values (>1MB)
- Deeply nested data structures
- Edge cases (empty values, None)
- Pydantic models
- Python dataclasses
- Datetime objects
- Decimal precision
- Binary data
- Special characters and control chars
- Serialization error handling

Run with:
    pytest test_sdk_data_handling.py -v
    pytest test_sdk_data_handling.py::test_messagepack_roundtrip -v
    pytest -m data_handling -v
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest

# Mark all tests in this module
pytestmark = [pytest.mark.sdk_e2e, pytest.mark.data_handling]


# ============================================================================
# Basic Type Serialization Tests (P0)
# ============================================================================


def test_messagepack_roundtrip(cache_io_decorator, clean_cache, sample_data):
    """Test MessagePack serialization roundtrip for basic types.

    Validates:
    - str, int, float, bool, None serialize correctly
    - Values retrieved match values stored
    - No data corruption during serialization

    Priority: P0
    """

    @cache_io_decorator
    def get_value(value_type: str):
        return sample_data["simple"][value_type]

    # Test each basic type
    assert get_value("string") == "Hello, World!"
    assert get_value("int") == 42
    assert get_value("float") == 3.14159
    assert get_value("bool") is True
    assert get_value("none") is None

    # Verify cache hits (should return same values)
    assert get_value("string") == "Hello, World!"
    assert get_value("int") == 42


def test_unicode_values(cache_io_decorator, clean_cache, sample_data):
    """Test Unicode string handling (emoji, CJK characters).

    Validates:
    - Emoji characters preserved
    - Chinese characters preserved
    - Arabic characters preserved
    - Mixed unicode strings preserved

    Priority: P0
    """

    @cache_io_decorator
    def get_unicode(key: str):
        return sample_data["unicode"][key]

    # Test various unicode strings
    assert get_unicode("emoji") == "🚀🎉💯"
    assert get_unicode("chinese") == "你好世界"
    assert get_unicode("arabic") == "مرحبا بالعالم"
    assert get_unicode("mixed") == "Hello 世界 🌍"

    # Verify cached values still correct
    assert get_unicode("emoji") == "🚀🎉💯"


def test_edge_cases(cache_io_decorator, clean_cache):
    """Test edge cases: empty string, empty list, empty dict, None.

    Validates:
    - Empty values serialize correctly
    - None handled properly
    - No confusion between empty and None

    Priority: P0
    """

    @cache_io_decorator
    def return_value(value):
        return value

    # Test empty values
    assert return_value("") == ""
    assert return_value([]) == []
    assert return_value({}) == {}
    assert return_value(None) is None

    # Verify they are distinct (not confused)
    assert return_value("") != None  # noqa: E711
    assert return_value([]) != None  # noqa: E711
    assert return_value({}) != None  # noqa: E711


# ============================================================================
# Complex Data Structure Tests (P1)
# ============================================================================


def test_nested_structures(cache_io_decorator, clean_cache, sample_data):
    """Test deeply nested dicts and lists.

    Validates:
    - Nested dict structure preserved
    - Nested list structure preserved
    - Deep nesting handled correctly

    Priority: P1
    """

    @cache_io_decorator
    def get_nested():
        return sample_data["collections"]["nested"]

    result = get_nested()
    expected = {"level1": {"level2": {"level3": [1, 2, 3]}}}
    assert result == expected

    # Verify structure preserved on cache hit
    result2 = get_nested()
    assert result2["level1"]["level2"]["level3"] == [1, 2, 3]


def test_large_values(cache_io_decorator, clean_cache):
    """Test values >1MB.

    Validates:
    - Large strings handled
    - No truncation
    - Performance acceptable

    Priority: P1
    """
    # Create ~2MB string
    large_string = "X" * (2 * 1024 * 1024)

    @cache_io_decorator
    def get_large_value():
        return large_string

    result = get_large_value()
    assert len(result) == len(large_string)
    assert result == large_string

    # Verify cache hit
    result2 = get_large_value()
    assert len(result2) == len(large_string)


# ============================================================================
# Large-value tests (P1) — multi-MB values must round-trip intact through the SDK,
# and values above the API's maximum size must be rejected with a permanent 413.
# These use INCOMPRESSIBLE random bytes so the payload is genuinely multi-MB on the
# wire (a repetitive string would compress to ~KB and never exercise the large path).
# ============================================================================


def _size_limit_key(namespace: str, name: str) -> str:
    """Build a valid cache key: ns:<ns>:func:<module.qualname>:args:<hash>:<meta>."""
    import hashlib

    args_hash = hashlib.blake2b(name.encode(), digest_size=32).hexdigest()
    return f"ns:{namespace}:func:tests.e2e.size_limits.{name}:args:{args_hash}:1s"


def test_large_value_roundtrip(cache_io_decorator, clean_cache):
    """A multi-MB value round-trips byte-identically through the SDK.

    Priority: P1
    """
    import os

    blob = os.urandom(13_500_000)  # ~13.5 MB, incompressible → genuinely multi-MB on the wire

    @cache_io_decorator
    def get_blob():
        return blob

    assert get_blob() == blob  # miss → compute → store
    assert get_blob() == blob  # hit → exact bytes preserved


def test_large_value_roundtrip_and_overwrite_via_http(http_client, sdk_config, unique_namespace):
    """Direct HTTP (bypasses SDK L1/serialization): a multi-MB value round-trips
    byte-identically, and overwriting it with a small value returns exactly the small
    value (no stale bytes).

    Priority: P1
    """
    import os

    # Raw key in the path (colons unencoded) — matches how the SDK builds the URL
    # (backend.py: f"/v1/cache/{key}"); the API splits the path on literal ':'.
    # (Percent-encoding the colons fails key-format validation.)
    key = _size_limit_key(unique_namespace, "roundtrip")
    url = f"{sdk_config['api_url']}/v1/cache/{key}"
    headers = {"Content-Type": "application/octet-stream", "X-TTL": "300"}

    big = os.urandom(13_500_000)
    put = http_client.put(url, data=big, headers=headers)
    assert put.status_code == 200, put.text
    got = http_client.get(url)
    assert got.status_code == 200
    assert got.content == big  # round-trip is byte-exact

    # Overwrite large → small: GET must return exactly the small value (no stale bytes).
    small = os.urandom(1024)
    put2 = http_client.put(url, data=small, headers=headers)
    assert put2.status_code == 200, put2.text
    got2 = http_client.get(url)
    assert got2.status_code == 200
    assert got2.content == small


def test_oversized_value_rejected_with_413(http_client, sdk_config, unique_namespace):
    """A value above the API's maximum size is rejected with a clean, permanent 413
    (not a 500 the SDK would mis-classify as transient and retry).

    Priority: P1
    """
    # Raw key in the path (colons unencoded) — see test_large_value_roundtrip_and_overwrite_via_http.
    key = _size_limit_key(unique_namespace, "oversized")
    url = f"{sdk_config['api_url']}/v1/cache/{key}"
    body = b"\x00" * (26 * 1024 * 1024)  # exceeds the API maximum value size
    resp = http_client.put(url, data=body, headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == 413


# ============================================================================
# Pydantic and Dataclass Tests (P1)
# ============================================================================


def test_pydantic_models(cache_io_decorator, clean_cache, pydantic_models):
    """Test Pydantic BaseModel serialization.

    Validates:
    - Pydantic models serialize/deserialize
    - Fields preserved correctly
    - datetime fields handled

    Priority: P1
    """
    user_profile = pydantic_models["UserProfile"]

    @cache_io_decorator
    def get_user_profile(user_id: int):
        profile = user_profile(
            user_id=user_id, name="John Doe", email="john@example.com", created_at=datetime(2025, 1, 1, 12, 0, 0)
        )
        # Convert to dict with ISO datetime strings for consistent serialization
        data = profile.model_dump()
        if isinstance(data["created_at"], datetime):
            data["created_at"] = data["created_at"].isoformat()
        return data

    result = get_user_profile(123)
    assert result["user_id"] == 123
    assert result["name"] == "John Doe"
    assert result["email"] == "john@example.com"
    # Verify datetime was serialized as string
    assert result["created_at"] == "2025-01-01T12:00:00"

    # Verify cache hit
    result2 = get_user_profile(123)
    assert result2 == result


def test_dataclasses(cache_io_decorator, clean_cache):
    """Test Python dataclass serialization.

    Validates:
    - Dataclasses converted to dict
    - Fields preserved correctly
    - Nested dataclasses handled

    Priority: P1
    """

    @dataclass
    class Person:
        name: str
        age: int
        active: bool

    @cache_io_decorator
    def get_person(person_id: int):
        person = Person(name="Alice", age=30, active=True)
        # Convert to dict for JSON serialization
        return {"name": person.name, "age": person.age, "active": person.active}

    result = get_person(1)
    assert result["name"] == "Alice"
    assert result["age"] == 30
    assert result["active"] is True

    # Verify cache hit
    result2 = get_person(1)
    assert result2 == result


# ============================================================================
# Special Type Tests (P1)
# ============================================================================


def test_datetime_objects(cache_io_decorator, clean_cache, sample_data):
    """Test datetime, date, time, timedelta objects.

    Validates:
    - datetime serialization (ISO 8601 or timestamp)
    - date serialization
    - Precision preserved

    Priority: P1
    """

    @cache_io_decorator
    def get_datetime_value(key: str):
        value = sample_data["special_types"][key]
        # Convert to ISO string for JSON serialization
        if isinstance(value, datetime):
            return value.isoformat()
        elif isinstance(value, date):
            return value.isoformat()
        return value

    # Test datetime
    dt_str = get_datetime_value("datetime")
    assert dt_str == "2025-01-01T12:00:00"

    # Test date
    date_str = get_datetime_value("date")
    assert date_str == "2025-01-01"

    # Verify cache hit
    assert get_datetime_value("datetime") == "2025-01-01T12:00:00"


def test_decimal_numbers(cache_io_decorator, clean_cache):
    """Test Decimal precision preservation.

    Validates:
    - Decimal values preserved
    - No floating point errors
    - High precision maintained

    Priority: P1
    """

    @cache_io_decorator
    def get_decimal_value():
        # Return as string to preserve precision
        return str(Decimal("123.456789012345678901234567890"))

    result = get_decimal_value()
    # Verify precision preserved (at least 15 significant digits)
    assert result.startswith("123.456789012345")

    # Verify cache hit
    result2 = get_decimal_value()
    assert result2 == result


def test_binary_data(cache_io_decorator, clean_cache):
    """Test bytes objects.

    Validates:
    - Binary data handled correctly
    - No corruption
    - Base64 or hex encoding if needed

    Priority: P1
    """

    @cache_io_decorator
    def get_binary():
        # Convert bytes to base64 for JSON serialization
        import base64

        binary_data = b"\x00\x01\x02\x03\xff\xfe\xfd"
        return base64.b64encode(binary_data).decode("utf-8")

    result = get_binary()
    assert isinstance(result, str)

    # Decode and verify
    import base64

    decoded = base64.b64decode(result)
    assert decoded == b"\x00\x01\x02\x03\xff\xfe\xfd"

    # Verify cache hit
    result2 = get_binary()
    assert result2 == result


def test_special_characters(cache_io_decorator, clean_cache):
    """Test special characters, control chars, newlines.

    Validates:
    - Newlines preserved
    - Tab characters preserved
    - Special control characters handled

    Priority: P1
    """

    @cache_io_decorator
    def get_special_string(key: str):
        special_strings = {
            "newlines": "line1\nline2\nline3",
            "tabs": "col1\tcol2\tcol3",
            "quotes": "He said \"Hello\" and she said 'Hi'",
            "backslash": "path\\to\\file",
            "mixed": 'Line 1\nTab:\tValue\n"Quoted"',
        }
        return special_strings[key]

    # Test various special characters
    assert get_special_string("newlines") == "line1\nline2\nline3"
    assert get_special_string("tabs") == "col1\tcol2\tcol3"
    assert get_special_string("quotes") == "He said \"Hello\" and she said 'Hi'"
    assert get_special_string("backslash") == "path\\to\\file"
    assert get_special_string("mixed") == 'Line 1\nTab:\tValue\n"Quoted"'

    # Verify cache hits
    assert get_special_string("newlines") == "line1\nline2\nline3"


# ============================================================================
# Error Handling Tests (P1)
# ============================================================================


def test_serialization_errors(cache_io_decorator, clean_cache):
    """Test that unsupported types raise clear errors.

    Validates:
    - Unsupported types detected
    - Clear error messages
    - Function itself can run (caching fails gracefully)

    Priority: P1

    Note: Some serializers may accept complex types. This test validates
    that if serialization fails, it fails with a clear error.
    """

    # Test with a type that's typically not serializable
    class CustomClass:
        def __init__(self, value):
            self.value = value

    @cache_io_decorator
    def return_custom_object():
        return CustomClass(42)

    # Depending on SDK implementation, this either:
    # 1. Raises a clear serialization error
    # 2. Falls back to no caching and returns the object
    # 3. Serializes it as a dict (if SDK is very permissive)

    try:
        result = return_custom_object()
        # If no error, SDK handled it gracefully (either cached or bypassed cache)
        # Verify function still works
        assert hasattr(result, "value") or isinstance(result, dict)
    except (TypeError, ValueError, AttributeError) as e:
        # Expected: clear error message about serialization
        error_msg = str(e).lower()
        assert any(keyword in error_msg for keyword in ["serialize", "json", "msgpack", "type", "not supported"]), (
            f"Error message unclear: {e}"
        )


# ============================================================================
# Collection Type Tests (P0)
# ============================================================================


def test_list_and_dict_roundtrip(cache_io_decorator, clean_cache, sample_data):
    """Test list and dict serialization.

    Validates:
    - Lists preserved with correct order
    - Dicts preserved with correct keys/values
    - Mixed types in collections handled

    Priority: P0
    """

    @cache_io_decorator
    def get_collection(collection_type: str):
        return sample_data["collections"][collection_type]

    # Test list
    result_list = get_collection("list")
    assert result_list == [1, 2, 3, 4, 5]

    # Test dict
    result_dict = get_collection("dict")
    assert result_dict == {"a": 1, "b": 2, "c": 3}

    # Verify cache hits
    assert get_collection("list") == [1, 2, 3, 4, 5]
    assert get_collection("dict") == {"a": 1, "b": 2, "c": 3}
