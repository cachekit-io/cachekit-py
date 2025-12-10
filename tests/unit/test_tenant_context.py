"""
Unit tests for tenant context extraction.

Tests all three tenant extractors (ArgumentNameExtractor, CallableExtractor, ContextVarExtractor)
and UUID format validation with FAIL CLOSED security policy.
"""

from __future__ import annotations

import uuid

import pytest

from cachekit.decorators.tenant_context import (
    ArgumentNameExtractor,
    CallableExtractor,
    ContextVarExtractor,
    _validate_tenant_id_format,
)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestArgumentNameExtractor:
    """Test ArgumentNameExtractor with various kwargs patterns."""

    def test_extract_from_kwargs_default_arg_name(self):
        """ArgumentNameExtractor should extract tenant_id from kwargs by default."""
        extractor = ArgumentNameExtractor()
        tenant_id = str(uuid.uuid4())
        args = ()
        kwargs = {"tenant_id": tenant_id, "user_id": 123}

        result = extractor.extract(args, kwargs)
        assert result == tenant_id

    def test_extract_from_kwargs_custom_arg_name(self):
        """ArgumentNameExtractor should extract using custom argument name."""
        extractor = ArgumentNameExtractor("org_id")
        org_id = str(uuid.uuid4())
        args = ()
        kwargs = {"org_id": org_id, "user_id": 456}

        result = extractor.extract(args, kwargs)
        assert result == org_id

    @pytest.mark.parametrize(
        "arg_name,kwargs,expected_uuid",
        [
            ("tenant_id", {"tenant_id": "550e8400-e29b-41d4-a716-446655440000"}, "550e8400-e29b-41d4-a716-446655440000"),
            ("org_id", {"org_id": "660f9511-f30c-52e5-b827-557766551111"}, "660f9511-f30c-52e5-b827-557766551111"),
            (
                "account_id",
                {"account_id": "770fa622-041d-63f6-c938-668877662222"},
                "770fa622-041d-63f6-c938-668877662222",
            ),
            ("client_id", {"client_id": str(uuid.uuid4())}, None),  # Will use generated UUID
        ],
    )
    def test_extract_various_arg_names(self, arg_name, kwargs, expected_uuid):
        """ArgumentNameExtractor should work with various argument names."""
        extractor = ArgumentNameExtractor(arg_name)
        args = ()

        # If expected_uuid is None, use the generated one from kwargs
        if expected_uuid is None:
            expected_uuid = kwargs[arg_name]

        result = extractor.extract(args, kwargs)
        assert result == expected_uuid

    def test_extract_fail_closed_missing_argument(self):
        """CRITICAL: ArgumentNameExtractor must raise ValueError when argument not found (FAIL CLOSED)."""
        extractor = ArgumentNameExtractor("tenant_id")
        args = ()
        kwargs = {"user_id": 123, "data_id": 456}  # Missing tenant_id

        with pytest.raises(ValueError, match="Tenant ID argument 'tenant_id' not found"):
            extractor.extract(args, kwargs)

    def test_extract_fail_closed_empty_kwargs(self):
        """CRITICAL: ArgumentNameExtractor must raise ValueError with empty kwargs (FAIL CLOSED)."""
        extractor = ArgumentNameExtractor("tenant_id")
        args = ()
        kwargs = {}

        with pytest.raises(ValueError, match="not found"):
            extractor.extract(args, kwargs)

    def test_extract_invalid_uuid_format_raises_error(self):
        """ArgumentNameExtractor must raise ValueError for invalid UUID format."""
        extractor = ArgumentNameExtractor()
        args = ()
        kwargs = {"tenant_id": "not-a-valid-uuid-format"}

        with pytest.raises(ValueError, match="must be valid UUID format"):
            extractor.extract(args, kwargs)

    def test_extract_converts_to_string(self):
        """ArgumentNameExtractor should convert tenant_id to string."""
        extractor = ArgumentNameExtractor()
        tenant_uuid = uuid.uuid4()
        args = ()
        kwargs = {"tenant_id": tenant_uuid}  # Pass UUID object

        result = extractor.extract(args, kwargs)
        assert isinstance(result, str)
        assert result == str(tenant_uuid)


class TestCallableExtractor:
    """Test CallableExtractor with custom extraction functions."""

    def test_extract_with_simple_callable(self):
        """CallableExtractor should use custom function to extract tenant_id."""

        def extract_fn(args, kwargs):
            return str(kwargs["org_id"])

        extractor = CallableExtractor(extract_fn)
        org_id = str(uuid.uuid4())
        args = ()
        kwargs = {"org_id": org_id}

        result = extractor.extract(args, kwargs)
        assert result == org_id

    def test_extract_from_nested_object(self):
        """CallableExtractor should extract from nested objects."""

        class Request:
            def __init__(self, user):
                self.user = user

        class User:
            def __init__(self, org_id):
                self.organization_id = org_id

        def extract_from_request(args, kwargs):
            request = kwargs.get("request")
            return str(request.user.organization_id)

        extractor = CallableExtractor(extract_from_request)
        org_id = str(uuid.uuid4())
        request = Request(User(org_id))
        args = ()
        kwargs = {"request": request}

        result = extractor.extract(args, kwargs)
        assert result == org_id

    @pytest.mark.parametrize(
        "extract_fn_name,expected_uuid",
        [
            ("extract_from_first_arg", "880fb733-152e-7407-d049-779988773333"),
            ("extract_from_kwargs", "990fc844-263f-8508-e15a-88aa99884444"),
        ],
    )
    def test_extract_various_patterns(self, extract_fn_name, expected_uuid):
        """CallableExtractor should work with various extraction patterns."""
        if extract_fn_name == "extract_from_first_arg":

            def extract_fn(args, kwargs):
                return str(args[0]) if args else str(kwargs["tenant_id"])

            args = (expected_uuid,)
            kwargs = {}
        else:  # extract_from_kwargs

            def extract_fn(args, kwargs):
                return str(kwargs["tenant_id"])

            args = ()
            kwargs = {"tenant_id": expected_uuid}

        extractor = CallableExtractor(extract_fn)
        result = extractor.extract(args, kwargs)
        assert result == expected_uuid

    def test_extract_validates_uuid_format(self):
        """CallableExtractor must validate UUID format from custom function."""

        def extract_invalid_uuid(args, kwargs):
            return "invalid-uuid-format"

        extractor = CallableExtractor(extract_invalid_uuid)
        args = ()
        kwargs = {}

        with pytest.raises(ValueError, match="must be valid UUID format"):
            extractor.extract(args, kwargs)

    def test_extract_callable_exception_propagates(self):
        """CallableExtractor should propagate exceptions from custom function."""

        def failing_extract(args, kwargs):
            raise KeyError("Missing required field")

        extractor = CallableExtractor(failing_extract)
        args = ()
        kwargs = {}

        with pytest.raises(KeyError, match="Missing required field"):
            extractor.extract(args, kwargs)

    def test_extract_from_header(self):
        """CallableExtractor should extract from request headers (common pattern)."""

        class Headers:
            def __init__(self, data):
                self._data = data

            def get(self, key):
                return self._data.get(key)

        class Request:
            def __init__(self, headers):
                self.headers = headers

        def extract_from_header(args, kwargs):
            request = kwargs.get("request")
            return request.headers.get("X-Tenant-ID")

        extractor = CallableExtractor(extract_from_header)
        tenant_id = str(uuid.uuid4())
        request = Request(Headers({"X-Tenant-ID": tenant_id}))
        args = ()
        kwargs = {"request": request}

        result = extractor.extract(args, kwargs)
        assert result == tenant_id


class TestContextVarExtractor:
    """Test ContextVarExtractor with contextvars (async-safe)."""

    def test_extract_from_context_var(self):
        """ContextVarExtractor should extract tenant_id from context variable."""
        tenant_id = str(uuid.uuid4())
        ContextVarExtractor.set_tenant_id(tenant_id)

        extractor = ContextVarExtractor()
        args = ()
        kwargs = {}

        result = extractor.extract(args, kwargs)
        assert result == tenant_id

    def test_set_tenant_id_validates_uuid(self):
        """ContextVarExtractor.set_tenant_id should validate UUID format."""
        with pytest.raises(ValueError, match="must be valid UUID format"):
            ContextVarExtractor.set_tenant_id("invalid-uuid")

    def test_extract_fail_closed_context_not_set(self):
        """CRITICAL: ContextVarExtractor must raise ValueError when context not set (FAIL CLOSED)."""
        # Create a new context var to ensure isolation from other tests
        import contextvars

        # Save original and create isolated context var for this test
        original_var = ContextVarExtractor._tenant_id_var
        ContextVarExtractor._tenant_id_var = contextvars.ContextVar("tenant_id_test")

        try:
            extractor = ContextVarExtractor()
            args = ()
            kwargs = {}

            with pytest.raises(ValueError, match="Tenant ID not set in context"):
                extractor.extract(args, kwargs)
        finally:
            # Restore original context var
            ContextVarExtractor._tenant_id_var = original_var

    @pytest.mark.parametrize(
        "tenant_uuid",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "660f9511-f30c-52e5-b827-557766551111",
            "770fa622-041d-63f6-c938-668877662222",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
        ],
    )
    def test_extract_various_uuids(self, tenant_uuid):
        """ContextVarExtractor should work with various UUID formats."""
        ContextVarExtractor.set_tenant_id(tenant_uuid)

        extractor = ContextVarExtractor()
        args = ()
        kwargs = {}

        result = extractor.extract(args, kwargs)
        assert result == tenant_uuid

    def test_context_var_isolation_across_calls(self):
        """ContextVarExtractor should maintain context isolation across calls."""
        tenant_id_1 = str(uuid.uuid4())
        tenant_id_2 = str(uuid.uuid4())

        extractor = ContextVarExtractor()
        args = ()
        kwargs = {}

        # Set first tenant_id
        ContextVarExtractor.set_tenant_id(tenant_id_1)
        result1 = extractor.extract(args, kwargs)
        assert result1 == tenant_id_1

        # Set second tenant_id (overwrites first)
        ContextVarExtractor.set_tenant_id(tenant_id_2)
        result2 = extractor.extract(args, kwargs)
        assert result2 == tenant_id_2

    def test_context_var_async_safe(self):
        """ContextVarExtractor should be async-safe (uses contextvars, not threading.local)."""
        # Verify it uses contextvars.ContextVar
        import contextvars

        assert isinstance(ContextVarExtractor._tenant_id_var, contextvars.ContextVar)


class TestUUIDFormatValidation:
    """Test UUID format validation with various inputs."""

    @pytest.mark.parametrize(
        "valid_uuid",
        [
            "550e8400-e29b-41d4-a716-446655440000",  # Standard UUID4
            "00000000-0000-0000-0000-000000000000",  # Nil UUID
            str(uuid.uuid4()),  # Generated UUID4
            str(uuid.uuid1()),  # UUID1
            str(uuid.uuid3(uuid.NAMESPACE_DNS, "test")),  # UUID3
            str(uuid.uuid5(uuid.NAMESPACE_DNS, "test")),  # UUID5
        ],
    )
    def test_validate_valid_uuid_formats(self, valid_uuid):
        """UUID validation should accept all valid UUID formats."""
        # Should not raise exception
        _validate_tenant_id_format(valid_uuid)

    @pytest.mark.parametrize(
        "invalid_uuid,expected_error",
        [
            ("not-a-uuid", "must be valid UUID format"),
            ("12345", "must be valid UUID format"),
            ("", "must be valid UUID format"),
            ("550e8400-e29b-41d4-a716", "must be valid UUID format"),  # Too short
            ("550e8400-e29b-41d4-a716-446655440000-extra", "must be valid UUID format"),  # Too long
            # Note: "550e8400e29b41d4a716446655440000" without hyphens IS valid - Python UUID() accepts it
            ("550e8400-e29b-41d4-a716-44665544000g", "must be valid UUID format"),  # Invalid hex char
            (None, "must be valid UUID format"),
            (123, "must be valid UUID format"),
        ],
    )
    def test_validate_invalid_uuid_formats(self, invalid_uuid, expected_error):
        """UUID validation should reject invalid UUID formats."""
        with pytest.raises(ValueError, match=expected_error):
            _validate_tenant_id_format(invalid_uuid)

    def test_validate_nil_uuid(self):
        """Nil UUID (all zeros) should be valid."""
        nil_uuid = "00000000-0000-0000-0000-000000000000"
        # Should not raise exception
        _validate_tenant_id_format(nil_uuid)

    def test_validate_uuid_object(self):
        """UUID objects should be validated correctly."""
        tenant_uuid = uuid.uuid4()
        # Should not raise exception when converted to string
        _validate_tenant_id_format(str(tenant_uuid))


class TestFailClosedBehavior:
    """Test FAIL CLOSED security policy across all extractors."""

    def test_argument_name_extractor_fail_closed(self):
        """ArgumentNameExtractor must fail closed when extraction fails."""
        extractor = ArgumentNameExtractor("missing_arg")
        args = ()
        kwargs = {"other_arg": "value"}

        with pytest.raises(ValueError, match="Cannot fall back to shared key"):
            extractor.extract(args, kwargs)

    def test_callable_extractor_fail_closed_on_exception(self):
        """CallableExtractor must fail closed when extraction raises exception."""

        def failing_extractor(args, kwargs):
            raise KeyError("Missing tenant information")

        extractor = CallableExtractor(failing_extractor)
        args = ()
        kwargs = {}

        with pytest.raises(KeyError):
            extractor.extract(args, kwargs)

    def test_context_var_extractor_fail_closed(self):
        """ContextVarExtractor must fail closed when context not set."""
        import contextvars

        # Create isolated context var for this test
        original_var = ContextVarExtractor._tenant_id_var
        ContextVarExtractor._tenant_id_var = contextvars.ContextVar("tenant_id_test")

        try:
            extractor = ContextVarExtractor()
            args = ()
            kwargs = {}

            with pytest.raises(ValueError, match="Cannot fall back to shared key"):
                extractor.extract(args, kwargs)
        finally:
            ContextVarExtractor._tenant_id_var = original_var

    def test_no_fallback_to_nil_uuid(self):
        """CRITICAL: Extractors must NOT fall back to nil UUID on failure."""
        import contextvars

        nil_uuid = "00000000-0000-0000-0000-000000000000"

        # ArgumentNameExtractor
        extractor1 = ArgumentNameExtractor("missing")
        with pytest.raises(ValueError):
            result = extractor1.extract((), {})
            # If it didn't raise, verify it's NOT the nil UUID
            assert result != nil_uuid, "Must not fall back to nil UUID"

        # ContextVarExtractor (without setting context) - use isolated context var
        original_var = ContextVarExtractor._tenant_id_var
        ContextVarExtractor._tenant_id_var = contextvars.ContextVar("tenant_id_test")
        try:
            extractor2 = ContextVarExtractor()
            with pytest.raises(ValueError):
                result = extractor2.extract((), {})
                # If it didn't raise, verify it's NOT the nil UUID
                assert result != nil_uuid, "Must not fall back to nil UUID"
        finally:
            ContextVarExtractor._tenant_id_var = original_var


class TestErrorMessages:
    """Test that error messages are actionable and clear."""

    def test_argument_name_extractor_error_message_clarity(self):
        """ArgumentNameExtractor error message should be actionable."""
        extractor = ArgumentNameExtractor("tenant_id")
        args = ()
        kwargs = {"user_id": 123}

        with pytest.raises(ValueError) as exc_info:
            extractor.extract(args, kwargs)

        error_msg = str(exc_info.value)
        assert "tenant_id" in error_msg
        assert "not found" in error_msg
        assert "security violation" in error_msg

    def test_context_var_extractor_error_message_clarity(self):
        """ContextVarExtractor error message should be actionable."""
        import contextvars

        # Use isolated context var for this test
        original_var = ContextVarExtractor._tenant_id_var
        ContextVarExtractor._tenant_id_var = contextvars.ContextVar("tenant_id_test")

        try:
            extractor = ContextVarExtractor()
            args = ()
            kwargs = {}

            with pytest.raises(ValueError) as exc_info:
                extractor.extract(args, kwargs)

            error_msg = str(exc_info.value)
            assert "not set in context" in error_msg
            assert "set_tenant_id" in error_msg
            assert "security violation" in error_msg
        finally:
            ContextVarExtractor._tenant_id_var = original_var

    def test_uuid_validation_error_message_clarity(self):
        """UUID validation error message should be actionable."""
        with pytest.raises(ValueError) as exc_info:
            _validate_tenant_id_format("invalid-uuid")

        error_msg = str(exc_info.value)
        assert "must be valid UUID format" in error_msg
        assert "550e8400" in error_msg  # Shows example UUID format
