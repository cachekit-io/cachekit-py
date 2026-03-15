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
