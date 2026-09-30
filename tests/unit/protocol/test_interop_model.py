"""Data-model edge behavior of cachekit.interop not pinned by the vendored vectors.

The protocol vectors (test_interop_vectors.py) byte-pin the canonical forms;
these tests pin the SDK-local model edges around them: msgpack 32-bit length
tiers, argument normalization of Python-idiomatic types (Enum/Path/Decimal),
``*args``/``**kwargs`` flattening, temporal value sentinels, strict
single-document decoding, the reserved-namespace boundary, and str-subclass
segments (validated and rendered as their exact str value).
"""

from __future__ import annotations

import inspect
import sys
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath

import pytest

from cachekit.interop import (
    InteropDecodeError,
    InteropError,
    args_hash,
    bind_flat_args,
    canonical_args_bytes,
    decode_interop_value,
    encode_interop_value,
    ensure_interop_backend_compatible,
    generate_interop_key,
    validate_interop_config,
)


class Color(Enum):
    RED = "red"


class TestThirtyTwoBitTiers:
    """Lengths above 0xFFFF select the msgpack *32 headers (str32/bin32/array32/map32).

    The vectors only exercise the small tiers; these pin the header byte and
    the 4-byte big-endian length so a tier regression cannot silently produce
    non-canonical (wrong-key) encodings for large payloads.
    """

    def test_str32(self):
        enc = canonical_args_bytes(["a" * 0x10000])
        # byte 0 is the fixarray(1) wrapper; the element starts at byte 1
        assert enc[1] == 0xDB
        assert enc[2:6] == (0x10000).to_bytes(4, "big")

    def test_bin32(self):
        enc = canonical_args_bytes([b"\x00" * 0x10000])
        assert enc[1] == 0xC6
        assert enc[2:6] == (0x10000).to_bytes(4, "big")

    def test_array32(self):
        enc = canonical_args_bytes([[0] * 0x10000])
        assert enc[1] == 0xDD
        assert enc[2:6] == (0x10000).to_bytes(4, "big")

    def test_map32(self):
        enc = canonical_args_bytes([{f"k{i:05d}": 0 for i in range(0x10000)}])
        assert enc[1] == 0xDF
        assert enc[2:6] == (0x10000).to_bytes(4, "big")


class TestArgNormalization:
    """Python-idiomatic argument types normalize to their canonical model form,
    so the same logical call hashes to the same cross-SDK key."""

    def test_enum_normalizes_to_value(self):
        assert args_hash([Color.RED]) == args_hash(["red"])

    def test_path_normalizes_to_posix_string(self):
        assert args_hash([PurePosixPath("/a/b")]) == args_hash(["/a/b"])

    def test_decimal_normalizes_to_string(self):
        assert args_hash([Decimal("1.10")]) == args_hash(["1.10"])

    def test_non_string_map_key_rejected(self):
        with pytest.raises(InteropError, match="map keys must be strings"):
            args_hash([{1: "a"}])


class TestBindFlatArgs:
    """``*args`` flattens to a nested array at its position, ``**kwargs`` to one map."""

    def test_var_positional_and_var_keyword_flatten(self):
        def f(a, *rest, **opts):
            pass

        sig = inspect.signature(f)
        assert bind_flat_args(sig, (1, 2, 3), {"x": 9}) == [1, [2, 3], {"x": 9}]

    def test_empty_variadics_flatten_to_empty_containers(self):
        def f(a, *rest, **opts):
            pass

        sig = inspect.signature(f)
        assert bind_flat_args(sig, (1,), {}) == [1, [], {}]

    def test_unbindable_call_raises_interop_error(self):
        def f(a):
            pass

        sig = inspect.signature(f)
        with pytest.raises(InteropError, match="do not bind"):
            bind_flat_args(sig, (1, 2), {})


class TestTemporalValueSentinels:
    """date/time values use the wire-format sentinel maps and revive on read."""

    def test_datetime_date_and_time_round_trip(self):
        value = {
            "dt": datetime(2026, 7, 20, 12, 0, 5, tzinfo=timezone.utc),
            "d": date(2026, 7, 20),
            "t": time(12, 30, 5),
        }
        assert decode_interop_value(encode_interop_value(value)) == value

    def test_value_map_non_string_key_rejected(self):
        with pytest.raises(InteropError, match="map keys must be strings"):
            encode_interop_value({1: "a"})


class TestStrictDecode:
    def test_malformed_document_raises_decode_error(self):
        # 0xc1 is the one byte the MessagePack spec never assigns
        with pytest.raises(InteropDecodeError, match="well-formed"):
            decode_interop_value(b"\xc1")


class TestBackendGuard:
    def test_none_backend_is_compatible(self):
        # lazily-resolved backends are re-checked per call after resolution
        ensure_interop_backend_compatible(None)


class TestReservedNamespaces:
    """``ns`` and ``nsapi`` are reserved as namespaces: the server parses a key
    starting ``ns:`` / ``nsapi:`` as namespace-prefixed. The reservation is
    exact-match and namespace-only."""

    @pytest.mark.parametrize("reserved", ["ns", "nsapi"])
    def test_reserved_namespace_rejected_by_keygen(self, reserved: str):
        with pytest.raises(InteropError, match="reserved"):
            generate_interop_key(reserved, "get_user", [1])

    @pytest.mark.parametrize("reserved", ["ns", "nsapi"])
    def test_reserved_namespace_rejected_by_config(self, reserved: str):
        with pytest.raises(InteropError, match="reserved"):
            validate_interop_config("get_user", reserved)

    @pytest.mark.parametrize(
        ("operation", "namespace"),
        [("ns", "users"), ("nsapi", "users"), ("get_user", "nsx"), ("get_user", "nsfw"), ("get_user", "nsapi2")],
    )
    def test_reservation_is_exact_match_and_namespace_only(self, operation: str, namespace: str):
        assert validate_interop_config(operation, namespace) == (operation, namespace)
        assert generate_interop_key(namespace, operation, [1]).startswith(f"{namespace}:{operation}:")


class TestDoubleDotSegments:
    """Neither segment may contain ``..``: the segment pattern admits it, but the
    server rejects ``..`` anywhere in a key. A lone ``.`` stays valid."""

    @pytest.mark.parametrize(
        ("operation", "namespace"),
        [("get_user", "a..b"), ("x..y", "users"), ("users.v1..beta", "users"), ("get_user", "a...b"), ("x..", "users")],
    )
    def test_double_dot_rejected_by_keygen_and_config(self, operation: str, namespace: str):
        with pytest.raises(InteropError, match=r"must not contain '\.\.'"):
            generate_interop_key(namespace, operation, [1])
        with pytest.raises(InteropError, match=r"must not contain '\.\.'"):
            validate_interop_config(operation, namespace)

    @pytest.mark.parametrize(
        ("operation", "namespace"),
        [("users.fetch.by_id", "app.v1"), ("get.", "users"), ("a.b.c", "x.y")],
    )
    def test_lone_dots_accepted(self, operation: str, namespace: str):
        assert validate_interop_config(operation, namespace) == (operation, namespace)
        assert generate_interop_key(namespace, operation, [1]).startswith(f"{namespace}:{operation}:")


class NS(str, Enum):
    USERS = "users"


class OP(str, Enum):
    GET_USER = "get_user"


class FormatsAsNsapi(str):
    """Value 'users', but formats as the reserved 'nsapi'."""

    def __format__(self, spec: str) -> str:
        return "nsapi"


class UnhashableAsReserved(str):
    """Value 'nsapi', but hashes and compares unlike it, so set membership misses."""

    def __hash__(self) -> int:
        return 0

    def __eq__(self, other: object) -> bool:
        return False


class TestSegmentSubclasses:
    """A ``str`` subclass is validated and rendered as its exact ``str`` value.

    Otherwise the checked string and the key string can differ: a ``(str, Enum)``
    member formats as ``NS.USERS`` since Python 3.11, a subclass can override
    ``__format__``, and ``__hash__``/``__eq__`` decide the reserved-set lookup.
    """

    PLAIN_KEY = generate_interop_key("users", "get_user", [42])

    def test_str_enum_mixin_renders_its_value(self):
        assert generate_interop_key(NS.USERS, OP.GET_USER, [42]) == self.PLAIN_KEY
        op, ns = validate_interop_config(OP.GET_USER, NS.USERS)
        assert (type(op), type(ns)) == (str, str)
        assert (op, ns) == ("get_user", "users")

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="enum.StrEnum is new in Python 3.11")
    def test_strenum_renders_its_value(self):
        from enum import StrEnum

        class Names(StrEnum):
            USERS = "users"
            GET_USER = "get_user"

        assert generate_interop_key(Names.USERS, Names.GET_USER, [42]) == self.PLAIN_KEY
        op, ns = validate_interop_config(Names.GET_USER, Names.USERS)
        assert (type(op), type(ns)) == (str, str)

    def test_overridden_format_never_mints_a_reserved_prefix(self):
        key = generate_interop_key(FormatsAsNsapi("users"), FormatsAsNsapi("get_user"), [42])
        assert key == self.PLAIN_KEY
        assert not key.startswith(("ns:", "nsapi:"))
        op, ns = validate_interop_config(FormatsAsNsapi("get_user"), FormatsAsNsapi("users"))
        assert f"{ns}:{op}" == "users:get_user"

    @pytest.mark.parametrize("reserved", ["ns", "nsapi"])
    def test_overridden_hash_cannot_skip_the_reservation(self, reserved: str):
        with pytest.raises(InteropError, match="reserved"):
            generate_interop_key(UnhashableAsReserved(reserved), "get_user", [1])
        with pytest.raises(InteropError, match="reserved"):
            validate_interop_config("get_user", UnhashableAsReserved(reserved))
