"""Interop mode (interop/v1): cross-SDK cache keys and values.

Implements protocol spec/interop-mode.md — the opt-in, language-neutral mode
that lets cachekit-py share cache entries with cachekit-rs and cachekit-ts:

- Keys: ``{namespace}:{operation}:{args_hash}`` where ``args_hash`` is
  Blake2b-256 over the canonical MessagePack encoding of the flat, bound
  argument array. No ``ns:`` prefix, no ``func:`` segment, no metadata flags.
- Values: one plain MessagePack document. No ByteStorage envelope, no LZ4,
  no xxHash3-64, never the Python-internal CK v3 frame.

The canonical encoder is a port of the protocol reference implementation
(protocol/tools/interop-reference.py) and is byte-verified against
test-vectors/interop-mode.json in tests/unit/protocol/test_interop_vectors.py.

WHY hand-rolled instead of msgpack.packb: interop/v1 requires shortest-form
widths, code-point-sorted map keys at every level, float64-only floats, and a
hard ban on ext types. packb happens to emit shortest forms today, but the
sort order, the number canonicalization (args profile), and the closed data
model are normative here — one explicit encoder keeps the whole contract in
one auditable place instead of splitting it between a pre-pass and library
behavior.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import re
import struct
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePath
from typing import Any
from uuid import UUID

import msgpack

from .serializers.base import SerializationError

# Full-string match REQUIRED: re.match with a $ anchor still accepts a
# trailing newline. Pinned by the reject_trailing_newline error vector.
SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

UINT64_MAX = 2**64 - 1
INT64_MIN = -(2**63)
# Exact float64 bounds for the integral-collapse range check. Both are powers
# of two, hence exactly representable; 2^64-1 is NOT (it rounds up to 2^64),
# so the upper comparison must be strict-less-than against 2^64.
_F64_UPPER_EXCL = 18446744073709551616.0  # 2^64
_F64_LOWER_INCL = -9223372036854775808.0  # -(2^63)

# CK v3 frame magic — a stored value starting with this is a Python-internal
# auto-mode entry, never an interop value (protocol#11 pins this diagnostic).
_CK_FRAME_MAGIC = b"CK"


class InteropError(ValueError):
    """A value, argument, or segment violates the interop/v1 data model.

    Raised at call time (keys) or store time (values). Deliberately NOT
    swallowed by the caching fallback paths: a value that hashes on one SDK
    and silently degrades on another is a cross-SDK cache-consistency bug,
    so interop mode fails loud (spec: "rejected with an error — never
    silently coerced or skipped").
    """


class InteropDecodeError(SerializationError):
    """Stored bytes are not a single well-formed MessagePack document.

    Subclasses SerializationError so the read path treats it as a cache miss
    and evicts the entry (self-healing overwrite), matching auto-mode
    corruption handling.
    """


class _PreEncoded:
    """A set normalized to its sorted, deduplicated, already-encoded elements."""

    __slots__ = ("encoded_elements",)

    def __init__(self, encoded_elements: list[bytes]) -> None:
        self.encoded_elements = encoded_elements


# ---------------------------------------------------------------------------
# Canonical MessagePack encoder — ONE encoder for both profiles.
# collapse_floats=True is the ARGS profile (number canonicalization);
# collapse_floats=False is the VALUE profile (floats always float64).
# ---------------------------------------------------------------------------


def _encode_int(n: int, out: bytearray) -> None:
    if not INT64_MIN <= n <= UINT64_MAX:
        raise InteropError(f"integer out of interop range [-2^63, 2^64-1]: {n}")
    if 0 <= n <= 0x7F:
        out.append(n)
    elif -32 <= n < 0:
        out.append(n & 0xFF)
    elif n > 0:
        if n <= 0xFF:
            out += b"\xcc" + n.to_bytes(1, "big")
        elif n <= 0xFFFF:
            out += b"\xcd" + n.to_bytes(2, "big")
        elif n <= 0xFFFFFFFF:
            out += b"\xce" + n.to_bytes(4, "big")
        else:
            out += b"\xcf" + n.to_bytes(8, "big")
    elif n >= -(2**7):
        out += b"\xd0" + n.to_bytes(1, "big", signed=True)
    elif n >= -(2**15):
        out += b"\xd1" + n.to_bytes(2, "big", signed=True)
    elif n >= -(2**31):
        out += b"\xd2" + n.to_bytes(4, "big", signed=True)
    else:
        out += b"\xd3" + n.to_bytes(8, "big", signed=True)


def _encode_str(s: str, out: bytearray) -> None:
    # str.encode raises UnicodeEncodeError on lone surrogates — the spec
    # requires rejecting non-well-formed strings, never replacement-encoding.
    try:
        b = s.encode("utf-8")
    except UnicodeEncodeError as e:
        raise InteropError(f"interop strings must be well-formed Unicode (no lone surrogates): {e}") from e
    n = len(b)
    if n <= 31:
        out.append(0xA0 | n)
    elif n <= 0xFF:
        out += b"\xd9" + n.to_bytes(1, "big")
    elif n <= 0xFFFF:
        out += b"\xda" + n.to_bytes(2, "big")
    else:
        out += b"\xdb" + n.to_bytes(4, "big")
    out += b


def _encode_bin(b: bytes, out: bytearray) -> None:
    n = len(b)
    if n <= 0xFF:
        out += b"\xc4" + n.to_bytes(1, "big")
    elif n <= 0xFFFF:
        out += b"\xc5" + n.to_bytes(2, "big")
    else:
        out += b"\xc6" + n.to_bytes(4, "big")
    out += b


def _encode_array_header(n: int, out: bytearray) -> None:
    if n <= 15:
        out.append(0x90 | n)
    elif n <= 0xFFFF:
        out += b"\xdc" + n.to_bytes(2, "big")
    else:
        out += b"\xdd" + n.to_bytes(4, "big")


def _encode_map_header(n: int, out: bytearray) -> None:
    if n <= 15:
        out.append(0x80 | n)
    elif n <= 0xFFFF:
        out += b"\xde" + n.to_bytes(2, "big")
    else:
        out += b"\xdf" + n.to_bytes(4, "big")


def _encode(v: object, out: bytearray, *, collapse_floats: bool) -> None:
    if v is None:
        out.append(0xC0)
    elif isinstance(v, bool):
        out.append(0xC3 if v else 0xC2)
    elif isinstance(v, int):
        _encode_int(v, out)
    elif isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise InteropError("NaN and Infinity are not allowed in interop mode")
        if collapse_floats and v.is_integer() and _F64_LOWER_INCL <= v < _F64_UPPER_EXCL:
            _encode_int(int(v), out)  # subsumes -0.0 -> int 0
        else:
            out += b"\xcb" + struct.pack(">d", v)
    elif isinstance(v, str):
        _encode_str(v, out)
    elif isinstance(v, (bytes, bytearray)):
        _encode_bin(bytes(v), out)
    elif isinstance(v, _PreEncoded):
        _encode_array_header(len(v.encoded_elements), out)
        for eb in v.encoded_elements:
            out += eb
    elif isinstance(v, (list, tuple)):
        _encode_array_header(len(v), out)
        for item in v:
            _encode(item, out, collapse_floats=collapse_floats)
    elif isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise InteropError(f"interop map keys must be strings, got {type(k).__name__}")
        # Unicode code point order == UTF-8 byte order; Python sorts str by code point.
        _encode_map_header(len(v), out)
        for k in sorted(v):
            _encode_str(k, out)
            _encode(v[k], out, collapse_floats=collapse_floats)
    else:
        raise InteropError(f"type {type(v).__name__} is not in the interop data model")


def _encode_canonical(value: object, *, collapse_floats: bool) -> bytes:
    out = bytearray()
    _encode(value, out, collapse_floats=collapse_floats)
    return bytes(out)


# ---------------------------------------------------------------------------
# Argument normalization (spec: The Interop Data Model, args profile)
# ---------------------------------------------------------------------------


def _normalize_arg(v: object) -> object:
    """Map a source argument into the interop data model. Recursive.

    Beyond the normative model, applies the spec's non-normative Python
    conveniences (Enum -> value, Path -> POSIX string, Decimal -> string).
    Decimal's textual form is caller-visible contract: "1.0" and "1.00" hash
    differently — agree on the form across SDKs or avoid Decimal arguments.
    """
    if v is None or isinstance(v, (bool, int, float, str, bytes, bytearray)):
        # Range/NaN/Infinity/surrogate enforcement lives in ONE place: the
        # encoder, which every path hits.
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise InteropError("naive datetimes are not allowed in interop arguments (timezone ambiguity)")
        # Integer microseconds since epoch (floored toward negative infinity —
        # exact for pre-epoch values too), then ONE float64 division by 10^6.
        # IEEE 754 division is exactly specified, so this is bit-identical
        # across languages (spec: DateTime determinism).
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = v - epoch
        micros = (delta.days * 86400 + delta.seconds) * 10**6 + delta.microseconds
        return micros / 1_000_000.0
    if isinstance(v, UUID):
        return str(v)  # lowercase hyphenated
    if isinstance(v, (set, frozenset)):
        # Sort by canonical-encoded bytes (total, language-neutral order);
        # dedupe post-normalization ({2, 2.0} collapses to a single int 2).
        encoded = sorted({_encode_canonical(_normalize_arg(e), collapse_floats=True) for e in v})
        return _PreEncoded(encoded)
    if isinstance(v, (list, tuple)):
        return [_normalize_arg(e) for e in v]
    if isinstance(v, dict):
        norm = {}
        for k, val in v.items():
            if not isinstance(k, str):
                raise InteropError(f"interop map keys must be strings, got {type(k).__name__}")
            norm[k] = _normalize_arg(val)
        return norm
    if isinstance(v, Enum):
        return _normalize_arg(v.value)
    if isinstance(v, (Path, PurePath)):
        return v.as_posix()
    if isinstance(v, Decimal):
        return str(v)
    raise InteropError(
        f"type {type(v).__name__} is not in the interop data model. "
        f"Supported argument types: None, bool, int, float, str, bytes, list, tuple, "
        f"dict (str keys), set, frozenset, tz-aware datetime, UUID, Enum, Path, Decimal."
    )


def canonical_args_bytes(args: list | tuple) -> bytes:
    """Canonical MessagePack encoding of the flat argument array (args profile)."""
    return _encode_canonical([_normalize_arg(a) for a in args], collapse_floats=True)


def args_hash(args: list | tuple) -> str:
    """Blake2b-256 (unkeyed, 32-byte digest) of the canonical argument bytes, lowercase hex."""
    return hashlib.blake2b(canonical_args_bytes(args), digest_size=32).hexdigest()


def validate_segment(name: str, segment: object) -> str:
    """Validate an interop namespace/operation segment. Returns the segment.

    Full-string match against ``^[a-z0-9][a-z0-9._-]{0,63}$`` — never silently
    normalized. re.fullmatch, not re.match: $ accepts a trailing newline.
    """
    if not isinstance(segment, str) or not SEGMENT_RE.fullmatch(segment):
        raise InteropError(
            f"invalid interop {name} {segment!r}: must full-string match ^[a-z0-9][a-z0-9._-]{{0,63}}$ "
            f"(lowercase ASCII letters, digits, '.', '_', '-'; 1-64 chars)"
        )
    return segment


def generate_interop_key(namespace: str, operation: str, args: list | tuple) -> str:
    """Build the interop/v1 cache key ``{namespace}:{operation}:{args_hash}``.

    ``args`` is the flat, bound argument array (see :func:`bind_flat_args`).
    Max possible key length is 64+1+64+1+64 = 194 chars — under every
    backend limit, so key truncation never applies to interop keys.
    """
    validate_segment("namespace", namespace)
    validate_segment("operation", operation)
    return f"{namespace}:{operation}:{args_hash(args)}"


def bind_flat_args(sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[Any]:
    """Bind a call to the signature and flatten to the canonical argument list.

    Named arguments bind to their declared positions; introspectable defaults
    are applied, so ``f(42)``, ``f(user_id=42)`` and ``f(42, flag=False)``
    (default False) all produce the same array. ``*args`` collects into one
    nested array at its declared position; ``**kwargs`` into one map.
    """
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError as e:
        raise InteropError(f"arguments do not bind to the function signature: {e}") from e
    bound.apply_defaults()
    flat: list[Any] = []
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            flat.append(list(bound.arguments.get(name, ())))
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            flat.append(dict(bound.arguments.get(name, {})))
        else:
            flat.append(bound.arguments[name])
    return flat


# ---------------------------------------------------------------------------
# Value codec (spec: Interop Value Format) — plain MessagePack, no envelope
# ---------------------------------------------------------------------------


def _normalize_value(v: object) -> object:
    """Map a value into the encodable model (value profile). Recursive.

    Temporal values become the wire-format.md sentinel maps — ordinary maps
    every MessagePack decoder can read; SDKs that know the convention revive
    native types. Note the asymmetry with ARGUMENT datetimes (Unix float64):
    keys need byte-equal hashes, values need round-trip fidelity.
    """
    if v is None or isinstance(v, (bool, int, float, str, bytes, bytearray)):
        return v
    if isinstance(v, datetime):
        return {"__datetime__": True, "value": v.isoformat()}
    if isinstance(v, date):
        return {"__date__": True, "value": v.isoformat()}
    if isinstance(v, time):
        return {"__time__": True, "value": v.isoformat()}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(e) for e in v]
    if isinstance(v, dict):
        norm = {}
        for k, val in v.items():
            if not isinstance(k, str):
                raise InteropError(f"interop value map keys must be strings, got {type(k).__name__}")
            norm[k] = _normalize_value(val)
        return norm
    raise InteropError(
        f"type {type(v).__name__} cannot be an interop value. Interop values are plain "
        f"MessagePack readable by every SDK: None, bool, int, float, str, bytes, list, "
        f"tuple, dict (str keys), datetime/date/time (sentinel maps). Python-specific "
        f"types (sets, custom classes, NumPy/pandas) do not round-trip cross-SDK."
    )


def encode_interop_value(value: Any) -> bytes:
    """Encode a value as one canonical plain-MessagePack document.

    Canonical (shortest forms, code-point-sorted map keys) to match the
    published value vectors. The value profile does NOT collapse integral
    floats — ``2.0`` stays float64 so it round-trips as a float.
    """
    return _encode_canonical(_normalize_value(value), collapse_floats=False)


def _iso_utc(value: str) -> str:
    """Normalize a trailing Z/z designator to +00:00.

    JS Date.toISOString() and Rust chrono RFC 3339 writers emit "…Z";
    datetime.fromisoformat only learned the Z designator in Python 3.11, and
    this SDK supports 3.10 — without this, every foreign datetime-bearing
    entry would decode-fail and be evicted by a 3.10 reader.
    """
    return value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value


def _revive_sentinels(obj: Any) -> Any:
    """msgpack object_hook: revive wire-format.md temporal sentinel maps."""
    if isinstance(obj, dict):
        if obj.get("__datetime__") is True and isinstance(obj.get("value"), str):
            return datetime.fromisoformat(_iso_utc(obj["value"]))
        if obj.get("__date__") is True and isinstance(obj.get("value"), str):
            return date.fromisoformat(obj["value"])
        if obj.get("__time__") is True and isinstance(obj.get("value"), str):
            return time.fromisoformat(_iso_utc(obj["value"]))
    return obj


def decode_interop_value(data: bytes | bytearray | memoryview) -> Any:
    """Decode one plain-MessagePack interop value document.

    Readers accept any well-formed MessagePack document (canonical or not),
    but MUST consume exactly one document — trailing bytes are rejected
    (msgpack-python raises ExtraData). A CK v3 frame prefix gets the
    protocol#11 diagnostic instead of decoding its magic byte as int 67.
    """
    raw = bytes(data)
    if raw[: len(_CK_FRAME_MAGIC)] == _CK_FRAME_MAGIC:
        raise InteropDecodeError(
            "stored value is a Python-SDK-internal auto-mode entry (CK v3 frame), not an "
            "interop value. An auto-mode writer and an interop reader are sharing a key — "
            "check that every writer for this key uses @cache(interop=...)."
        )
    try:
        return msgpack.unpackb(raw, raw=False, strict_map_key=True, object_hook=_revive_sentinels)
    except Exception as e:
        raise InteropDecodeError(f"stored value is not a single well-formed MessagePack document: {e}") from e


# ---------------------------------------------------------------------------
# Backend compatibility guard (fail closed — CWE-636)
# ---------------------------------------------------------------------------


def ensure_interop_backend_compatible(backend: Any) -> None:
    """Fail closed when the backend rewrites keys on the wire.

    A backend key prefix (e.g. MemcachedBackend's ``key_prefix``) makes this
    SDK read/write ``{prefix}{ns}:{op}:{hash}`` while other SDKs use the bare
    key — silent cross-SDK misses, and the encryption AAD binds the
    UN-prefixed key. Backends that transform keys MUST expose the prefix as a
    ``key_prefix`` attribute (contract); non-prefix key transforms are
    incompatible with interop mode entirely. Same decision as cachekit-ts
    (wrap-time + per-call guard) and cachekit-rs (read-time guard).

    Called at decoration time when the backend is known, and re-checked
    per call after lazy backend resolution — a construction-time snapshot
    alone would fail open against a dynamically-changing prefix.
    """
    if backend is None:
        return
    prefix = getattr(backend, "key_prefix", None) or getattr(backend, "_key_prefix", None)
    if prefix:
        from .config.validation import ConfigurationError

        raise ConfigurationError(
            f"interop mode cannot be used with a key-prefixing backend "
            f"({type(backend).__name__} has key_prefix={prefix!r}): the prefix would be "
            f"invisible to other SDKs and break cross-SDK key identity. Pass an explicit "
            f"non-prefixing backend (e.g. RedisBackend()) instead — the default provider's "
            f"Redis path is a tenant-scoped wrapper and cannot serve interop entries."
        )


__all__ = [
    "InteropDecodeError",
    "InteropError",
    "SEGMENT_RE",
    "args_hash",
    "bind_flat_args",
    "canonical_args_bytes",
    "decode_interop_value",
    "encode_interop_value",
    "ensure_interop_backend_compatible",
    "generate_interop_key",
    "validate_segment",
]
