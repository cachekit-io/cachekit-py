"""Data-model edge behavior of cachekit.interop not pinned by the vendored vectors.

The protocol vectors (test_interop_vectors.py) byte-pin the canonical forms;
these tests pin the SDK-local model edges around them: msgpack 32-bit length
tiers, argument normalization of Python-idiomatic types (Enum/Path/Decimal),
``*args``/``**kwargs`` flattening, temporal value sentinels, strict
single-document decoding, the reserved-namespace boundary, str-subclass
segments (validated and rendered as their exact str value), and scalar-subclass
arguments (hashed as their exact base-type value).
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


class AsAdminStr(str):
    def encode(self, *args, **kwargs) -> bytes:
        return b"admin"


class LyingToBytes(int):
    def to_bytes(self, *args, **kwargs) -> bytes:
        return b"\xff" * args[0]


class LyingCompare(int):
    def __le__(self, other: object) -> bool:
        return False

    def __lt__(self, other: object) -> bool:
        return False

    def __gt__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return False


class LyingIsInteger(float):
    def is_integer(self) -> bool:
        return False


class LyingInt(float):
    def __int__(self) -> int:
        return 99


class AsAdminBytes(bytes):
    def __bytes__(self) -> bytes:
        return b"admin"


class AsAdminBytearray(bytearray):
    def __bytes__(self) -> bytes:
        return b"admin"


class LtOnly(str):
    def __lt__(self, other: object) -> bool:
        return True


class EqualsPlainA(str):
    """Value 'a', but hashes unlike 'a', so a dict holds it beside a plain 'a' key."""

    def __hash__(self) -> int:
        return 1


def _reports(cls: type):
    """A __class__ property that makes isinstance(obj, cls) true without subclassing cls."""
    return property(lambda self: cls)


class IntReportsStr(int):
    __class__ = _reports(str)  # type: ignore[assignment]

    def to_bytes(self, *args, **kwargs) -> bytes:
        return (int(self) + 1).to_bytes(*args, **kwargs)

    def encode(self, *args, **kwargs) -> bytes:
        return b"admin"


class BytesReportsStr(bytes):
    __class__ = _reports(str)  # type: ignore[assignment]

    def encode(self, *args, **kwargs) -> bytes:
        return b"admin"


class StrReportsBool(str):
    __class__ = _reports(bool)  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return True


class IntReportsBool(int):
    __class__ = _reports(bool)  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return True


class ForwardingProxy:
    """Reports its target's type through __class__ and forwards attributes, like wrapt's ObjectProxy."""

    def __init__(self, target: object) -> None:
        object.__setattr__(self, "_target", target)

    @property
    def __class__(self):  # type: ignore[override]
        return type(object.__getattribute__(self, "_target"))

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_target"), name)


class TestArgSubclasses:
    """A ``str``/``int``/``float``/``bytes`` subclass argument hashes as its exact base-type value.

    The encoder would otherwise call the subclass's own ``encode``, ``to_bytes``,
    ``is_integer``, ``__int__``, ``__bytes__`` or ``__lt__``, so the hashed bytes
    need not be the argument's value — ``AsAdminStr("user")`` would read the
    entry cached for ``"admin"``.
    """

    @pytest.mark.parametrize(
        ("arg", "plain", "forged"),
        [
            (AsAdminStr("user"), "user", "admin"),
            (IntReportsStr(300), 300, "admin"),
            (BytesReportsStr(b"user"), b"user", "admin"),
            (StrReportsBool("admin"), "admin", True),
            (IntReportsBool(5), 5, True),
            ({"k": StrReportsBool("admin")}, {"k": "admin"}, {"k": True}),
            ({StrReportsBool("admin")}, {"admin"}, {True}),
            (LyingToBytes(1000), 1000, None),
            (LyingIsInteger(2.0), 2.0, None),
            (LyingInt(3.0), 3, 99),
            (AsAdminBytes(b"user"), b"user", b"admin"),
            (AsAdminBytearray(b"user"), b"user", b"admin"),
            ([AsAdminStr("user")], ["user"], ["admin"]),
            ({"k": AsAdminStr("user")}, {"k": "user"}, {"k": "admin"}),
            ({AsAdminStr("user"): 1}, {"user": 1}, {"admin": 1}),
            ({"a": 1, LtOnly("b"): 2}, {"a": 1, "b": 2}, None),
            ({AsAdminStr("user")}, {"user"}, {"admin"}),
        ],
        ids=[
            "str.encode",
            "int-reports-str",
            "bytes-reports-str",
            "str-reports-bool",
            "int-reports-bool",
            "dict-value-reports-bool",
            "set-element-reports-bool",
            "int.to_bytes",
            "float.is_integer",
            "float.__int__",
            "bytes.__bytes__",
            "bytearray.__bytes__",
            "nested-list-element",
            "dict-value",
            "dict-key",
            "dict-key-__lt__",
            "set-element",
        ],
    )
    def test_hashes_like_the_equal_plain_value(self, arg, plain, forged):
        assert args_hash([arg]) == args_hash([plain])
        if forged is not None:
            assert args_hash([arg]) != args_hash([forged])

    def test_lying_int_comparisons_hash_like_the_plain_value(self):
        assert args_hash([LyingCompare(300)]) == args_hash([300])

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="enum.StrEnum is new in Python 3.11")
    def test_strenum_hashes_like_its_value(self):
        from enum import StrEnum

        class Role(StrEnum):
            ADMIN = "admin"

        assert args_hash([Role.ADMIN]) == args_hash(["admin"])

    def test_intenum_hashes_like_its_value(self):
        from enum import IntEnum

        class Level(IntEnum):
            HIGH = 3

        assert args_hash([Level.HIGH]) == args_hash([3])

    def test_str_enum_mixin_hashes_like_its_value(self):
        assert args_hash([OP.GET_USER]) == args_hash(["get_user"])

    def test_keys_equal_after_normalization_raise(self):
        arg = {EqualsPlainA("a"): 1, "a": 2}
        assert len(arg) == 2  # the dict really holds two keys
        # Collapsing to either value would hash it like {"a": 1} or {"a": 2}.
        with pytest.raises(InteropError, match="duplicate key 'a'"):
            args_hash([arg])

    def test_non_str_key_reporting_str_is_rejected(self):
        assert isinstance(IntReportsStr(5), str)
        with pytest.raises(InteropError, match="map keys must be strings"):
            args_hash([{IntReportsStr(5): 1}])

    def test_forwarding_proxy_still_hashes_like_its_target(self):
        # Not a subclass: the base-type slots would raise TypeError on it, so it
        # keeps its pre-existing pass-through rather than breaking lazy proxies.
        proxy = ForwardingProxy("user")
        assert isinstance(proxy, str)
        assert args_hash([proxy]) == args_hash(["user"])
