# pyright: reportOptionalMemberAccess=false
# pyright: reportInvalidTypeForm=false
"""AutoSerializer - Intelligent type-detecting serializer.

Auto-detects and optimizes:
- NumPy arrays (NUMPY_RAW format, zero-copy)
- Pandas DataFrames (columnar format, 60%+ faster than pickle)
- Pandas Series (metadata preservation)
- datetime/date/time (ISO-8601)
- UUID (string representation)
- set/frozenset (type-safe roundtrip)
- tuple (recursive type-safe roundtrip)

Uses MessagePack as the default format with graceful degradation for optional dependencies.

Type Checking Note:
Optional imports (numpy, pandas) are guarded at runtime by HAS_NUMPY, HAS_PANDAS flags.
Type checker cannot statically verify these; suppressed via pyright config comments above.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, ClassVar, Optional
from uuid import UUID

import msgpack
import xxhash

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

# Optional imports with feature flags
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None  # type: ignore[assignment]

try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    pd = None  # type: ignore[assignment]

# Optional: ArrowSerializer for fast DataFrame serialization
try:
    from .arrow_serializer import ArrowSerializer

    HAS_ARROW_SERIALIZER = True
except ImportError:
    HAS_ARROW_SERIALIZER = False
    ArrowSerializer = None  # type: ignore[assignment,misc]

from cachekit._rust_serializer import ByteStorage, EnvelopeIntegrityError
from cachekit.hash_utils import redact_error_for_log

from .base import (
    PAYLOAD_DECODE_ERRORS,
    EnvelopeShapeError,
    SerializationError,
    SerializationFormat,
    SerializationMetadata,
    bounded_error,
    unpackb_bounded,
)

logger = logging.getLogger(__name__)

# Every `format` a ByteStorage envelope can carry out of serialize(): _serialize_msgpack stores
# `self.default_format` ("msgpack", enforced in __init__), _serialize_dataframe "dataframe",
# _serialize_series "series". Those are the only three ByteStorage.store call sites.
_ENVELOPE_FORMATS = frozenset({"msgpack", "dataframe", "series"})

# Error message constants for unsupported types
PYDANTIC_ERROR_MESSAGE = (
    "AutoSerializer does not support Pydantic models. Use .model_dump() to convert to dict: result = model.model_dump()"
)

ORM_ERROR_MESSAGE = (
    "AutoSerializer does not support ORM models (SQLAlchemy, Django, etc.). Convert to dict or implement a custom serializer."
)

CUSTOM_CLASS_ERROR_MESSAGE = (
    "AutoSerializer does not support custom classes. "
    "Supported types: dict, list, tuple, str, int, float, bool, None, bytes, "
    "datetime, date, time, UUID, set, frozenset, NumPy arrays, pandas DataFrames.\n"
    "Options:\n"
    "  1. Convert to dict manually\n"
    "  2. Use dataclasses.asdict() for dataclasses\n"
    "  3. Write a custom serializer implementing SerializerProtocol"
)


def _safe_hasattr(obj: Any, attr: str) -> bool:
    """Safe hasattr that prevents arbitrary code execution via __getattr__.

    Standard hasattr() can trigger side effects if the object implements
    __getattr__ or if the attribute is a property with side effects.

    This implementation uses object.__getattribute__() to bypass custom
    __getattr__ implementations, preventing malicious code execution.

    Args:
        obj: Object to check for attribute
        attr: Attribute name to check

    Returns:
        True if attribute exists, False otherwise (including on errors)

    Security:
        Prevents DoS attacks via expensive property evaluation or
        malicious __getattr__ implementations.

    Example:
        >>> class Evil:
        ...     def __getattr__(self, name):
        ...         import os
        ...         os.system('rm -rf /')  # Malicious!
        ...         return lambda: {}
        >>> _safe_hasattr(Evil(), 'model_dump')  # Safe - returns False
        False
    """
    try:
        # Use object.__getattribute__ to bypass custom __getattr__
        # This prevents triggering malicious code in __getattr__ implementations
        object.__getattribute__(obj, attr)
        return True
    except AttributeError:
        # Attribute doesn't exist - this is the normal case
        return False
    except Exception:
        # Any other exception means we can't trust the object
        return False


def _wrap_tuples(obj: Any) -> Any:
    """Recursively wrap tuples in type markers before msgpack encoding.

    Msgpack natively serializes tuples as arrays (same as lists), so the
    ``default`` callback is never called for them. This pre-processor
    converts tuples to ``{"__tuple__": True, "value": [...]}`` markers
    that ``_auto_object_hook`` restores on deserialization.

    Only affects tuples — all other types pass through unchanged and are
    handled by msgpack's ``default`` callback (``_auto_default``).
    """
    if isinstance(obj, tuple):
        return {"__tuple__": True, "value": [_wrap_tuples(x) for x in obj]}
    if isinstance(obj, list):
        return [_wrap_tuples(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _wrap_tuples(v) for k, v in obj.items()}
    return obj


def _is_plain_numpy_numeric(dtype: Any) -> bool:
    """True only for a plain NumPy int/uint/float dtype.

    Pandas nullable/extension dtypes (Int64, Float64, boolean, pyarrow-backed, ...)
    are excluded: their backing arrays have no ``.tobytes()`` (AttributeError), and
    dtype-name matching is unreliable ("Int64" misses ``startswith("int")`` while
    "int64[pyarrow]" wrongly matches it). Used by the no-pyarrow columnar fallback.
    """
    return HAS_PANDAS and not pd.api.types.is_extension_array_dtype(dtype) and dtype.kind in ("i", "u", "f")


def _dtype_from_untrusted(spec: Any, *, numeric_only: bool = False) -> np.dtype:
    """``np.dtype(spec)`` for a dtype the cache entry itself supplies, refusing what the writer never emits.

    A forged ``M8[0ns]`` (zero datetime unit multiplier) passes ``np.frombuffer`` and then kills
    the process with SIGFPE inside pandas — a signal no ``except`` can catch — so it is refused
    before any array is built. Columnar (DataFrame/Series) entries only ever carry dtypes that
    pass ``_is_plain_numpy_numeric``, the write-side predicate, so ``numeric_only`` mirrors it.
    """
    dtype = np.dtype(spec)
    if numeric_only and not _is_plain_numpy_numeric(dtype):
        raise SerializationError(
            f"Forged columnar dtype {bounded_error(str(dtype))}: the writer only emits plain NumPy numeric columns"
        )
    if dtype.kind in "Mm" and np.datetime_data(dtype)[1] == 0:
        raise SerializationError(f"Forged dtype {bounded_error(str(dtype))}: a zero datetime unit multiplier crashes pandas")
    return dtype


def _expect(value: Any, kind: type, what: str) -> Any:
    """Refuse a columnar field whose type the writer never emits.

    The ``__ndarray__`` object hook can substitute an attacker-typed ndarray for any field of a
    forged DataFrame/Series document; pandas then asserts (``AssertionError``) or indexing raises
    ``IndexError`` — both outside ``PAYLOAD_DECODE_ERRORS``. The writer emits ``list`` for
    ``columns`` / ``index`` / object data and ``dict`` for the document and each column.
    """
    if not isinstance(value, kind):
        raise SerializationError(f"Forged columnar payload: {what} is {type(value).__name__}, expected {kind.__name__}")
    return value


def _column_values(info: dict[str, Any], what: str) -> Any:
    """Rebuild one column's values from the ``{type, data[, dtype]}`` the writer emits (``dtype`` only for ``"numeric"``).

    ``type`` is an allow-list, not a numeric/else switch: an unknown marker must not be read as object data.
    """
    marker = info["type"]
    if marker == "numeric":
        # .copy() → writable values that do not alias the source buffer (#157).
        return np.frombuffer(info["data"], dtype=_dtype_from_untrusted(info["dtype"], numeric_only=True)).copy()
    if marker == "object":
        return _expect(info["data"], list, f"{what} data")
    # Attacker-chosen: echo a str capped at 40 chars; never repr() a structure (RecursionError on 3.10/3.11 at depth ~1000).
    shown = marker if isinstance(marker, str) else type(marker).__name__
    raise SerializationError(f"Forged columnar payload: {what} type is {shown!r:.40}, expected 'numeric' or 'object'")


def _column_trio(series: Any) -> dict[str, Any]:
    """Build one column's/Series' marker — the write-side mirror of :func:`_column_values`.

    The marker is the 3-key ``{type: "numeric", data, dtype}`` for a plain-numeric column and the
    2-key ``{type: "object", data}`` otherwise (the ``dtype`` key is numeric-only) — "trio" names
    the maximal numeric form. One writer for both the DataFrame-column and the bare-Series paths, so
    the marker set and key order live in a single place and a third marker cannot be added to one
    side only (the AC3 goal). Plain NumPy
    numeric dtypes take the raw-buffer path; everything else (nullable/extension dtypes) takes the
    NA-safe object path so pd.NA/NaT do not crash msgpack (#160). Wire bytes and key order MUST
    stay byte-identical to the interop fixtures.
    """
    if _is_plain_numpy_numeric(series.dtype):
        return {"type": "numeric", "data": series.values.tobytes(), "dtype": str(series.dtype)}  # type: ignore[union-attr]
    return {"type": "object", "data": _na_safe_object_list(series)}


def _na_safe_object_list(series: Any) -> list:
    """``series.tolist()`` with scalar pandas NA sentinels (pd.NA/NaT/NaN) mapped to None.

    msgpack cannot pack pd.NA/NaT. The column-level ``isna()`` mask is used (rather than
    per-element ``pd.isna``) to avoid ambiguity on object cells that are themselves
    array-like (e.g. a list value). Datetime objects are preserved for the custom encoder.
    """
    na_mask = series.isna().tolist()
    raw = series.astype(object).tolist()
    return [None if is_na else value for is_na, value in zip(na_mask, raw, strict=True)]


def _auto_default(obj: Any) -> Any:
    """Custom encoder for types not natively supported by MessagePack.

    Handles:
    - datetime/date/time → ISO-8601 strings
    - UUID → string representation
    - set/frozenset → list (with type marker for roundtrip)
    - NumPy arrays → dict with binary data, shape, and dtype (nested in dicts/lists)

    Provides helpful errors for:
    - Pydantic models (suggest .model_dump())
    - ORM models (suggest conversion to dict)
    - Custom classes (suggest alternatives)

    Args:
        obj: Object to encode

    Returns:
        MessagePack-compatible representation

    Raises:
        TypeError: For unsupported types with actionable guidance
    """
    # Existing: datetime/date/time support (KEEP)
    if isinstance(obj, datetime):
        return {"__datetime__": True, "value": obj.isoformat()}
    if isinstance(obj, date):
        return {"__date__": True, "value": obj.isoformat()}
    if isinstance(obj, time):
        return {"__time__": True, "value": obj.isoformat()}

    # NEW: UUID support
    if isinstance(obj, UUID):
        return {"__uuid__": True, "value": str(obj)}

    # NEW: set/frozenset support (type-safe roundtrip)
    if isinstance(obj, (set, frozenset)):
        return {"__set__": True, "value": list(obj), "frozen": isinstance(obj, frozenset)}

    # NumPy array support (nested in dicts/lists via msgpack custom encoder)
    if HAS_NUMPY and isinstance(obj, np.ndarray):
        return {"__ndarray__": True, "data": obj.tobytes(), "shape": list(obj.shape), "dtype": str(obj.dtype)}

    # NEW: Helpful error detection for common unsupported types
    if _safe_hasattr(obj, "model_dump"):  # Pydantic BaseModel
        raise TypeError(PYDANTIC_ERROR_MESSAGE)

    if _safe_hasattr(obj, "__tablename__"):  # SQLAlchemy/ORM model
        raise TypeError(ORM_ERROR_MESSAGE)

    if _safe_hasattr(obj, "__dict__") and type(obj).__module__ != "builtins":
        # Custom class (has __dict__ but not a builtin type)
        raise TypeError(CUSTOM_CLASS_ERROR_MESSAGE)

    # Generic MessagePack error (fallback)
    raise TypeError(f"Object of type {type(obj).__name__} is not MessagePack serializable")


def _auto_object_hook(obj: Any) -> Any:
    """Custom decoder for types encoded by _auto_default().

    Restores:
    - datetime/date/time from ISO-8601 strings
    - UUID from string representation
    - set/frozenset from list (type-safe roundtrip)
    - NumPy arrays from binary data with shape and dtype

    Args:
        obj: Object from MessagePack decoder

    Returns:
        Restored Python object or original obj if not a special marker
    """
    if isinstance(obj, dict):
        # Strict validation to prevent collision with user dicts like {'__time__': UUID(...)}
        # Only decode if marker is exactly True (not any truthy value)
        # Raise error if marker is True but structure is malformed (corrupted cache data)

        if obj.get("__datetime__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid datetime format: missing 'value' field in cached data")
            return datetime.fromisoformat(obj["value"])

        if obj.get("__date__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid date format: missing 'value' field in cached data")
            return date.fromisoformat(obj["value"])

        if obj.get("__time__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid time format: missing 'value' field in cached data")
            return time.fromisoformat(obj["value"])

        if obj.get("__uuid__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid UUID format: missing 'value' field in cached data")
            value = obj["value"]
            try:
                return UUID(value)
            except (ValueError, TypeError) as e:
                raise SerializationError(f"Invalid UUID format in cached data: {bounded_error(str(value))}") from e

        if obj.get("__tuple__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid tuple format: missing 'value' field in cached data")
            value_list = obj["value"]
            if not isinstance(value_list, list):
                raise SerializationError(f"Invalid tuple format: expected list, got {type(value_list).__name__}")
            return tuple(value_list)

        if obj.get("__set__") is True:
            if "value" not in obj:
                raise SerializationError("Invalid set format: missing 'value' field in cached data")
            value_list = obj["value"]
            if not isinstance(value_list, list):
                raise SerializationError(f"Invalid set format: expected list, got {type(value_list).__name__}")

            if obj.get("frozen"):
                return frozenset(value_list)
            else:
                return set(value_list)

        if obj.get("__ndarray__") is True:
            if not HAS_NUMPY:
                raise SerializationError("Cannot deserialize numpy array: numpy is not installed")
            if "data" not in obj or "shape" not in obj or "dtype" not in obj:
                raise SerializationError("Invalid ndarray format: missing required fields in cached data")
            # .copy(): writable result that does not alias the source buffer (the L1-cached bytes on a hit) — #157.
            return np.frombuffer(obj["data"], dtype=_dtype_from_untrusted(obj["dtype"])).reshape(obj["shape"]).copy()

    return obj


class AutoSerializer:
    """Intelligent serializer with automatic type detection.

    Implements SerializerProtocol via structural subtyping (PEP 544).
    No inheritance required - protocol compliance validated at runtime.

    Features:
    - MessagePack as default format
    - Automatic NumPy array detection and optimization
    - Automatic DataFrame detection and optimization
    - datetime/date/time support (ISO-8601)
    - UUID support (string representation)
    - set/frozenset support (type-safe roundtrip)
    - LZ4 compression via Rust layer
    - xxHash3-64 checksums for integrity
    - ZERO backwards compatibility (greenfield)

    Named "Auto" to be transparent about auto-detection behavior.
    Users understand: "This serializer makes intelligent guesses about optimization."

    Protocol Compliance:
        serialize(obj) -> tuple[bytes, SerializationMetadata]
        deserialize(data, metadata=None) -> Any

    Examples:
        Basic roundtrip with dict:

        >>> serializer = AutoSerializer()
        >>> data, meta = serializer.serialize({"user": "alice", "score": 100})
        >>> isinstance(data, bytes)
        True
        >>> meta.format.value
        'msgpack'
        >>> result = serializer.deserialize(data, meta)
        >>> result == {"user": "alice", "score": 100}
        True

        UUID preservation:

        >>> from uuid import UUID
        >>> original = {"id": UUID("12345678-1234-5678-1234-567812345678")}
        >>> data, meta = serializer.serialize(original)
        >>> result = serializer.deserialize(data, meta)
        >>> result["id"] == original["id"]
        True
        >>> isinstance(result["id"], UUID)
        True

        Set/frozenset roundtrip:

        >>> original = {"tags": {"a", "b", "c"}, "frozen": frozenset([1, 2])}
        >>> data, meta = serializer.serialize(original)
        >>> result = serializer.deserialize(data, meta)
        >>> result["tags"] == {"a", "b", "c"}
        True
        >>> isinstance(result["frozen"], frozenset)
        True

        Datetime support:

        >>> from datetime import datetime
        >>> dt = datetime(2024, 6, 15, 10, 30, 0)
        >>> data, meta = serializer.serialize({"created": dt})
        >>> result = serializer.deserialize(data, meta)
        >>> result["created"] == dt
        True

        Disable integrity checking for speed:

        >>> fast_serializer = AutoSerializer(enable_integrity_checking=False)
        >>> data, _ = fast_serializer.serialize({"fast": True})
        >>> fast_serializer.deserialize(data)
        {'fast': True}
    """

    # AutoSerializer emits Python-specific type tags (set, frozenset, UUID, tuple) that no
    # other-language SDK can decode — single-SDK only, so it is rejected under encryption.
    cross_sdk_compatible: ClassVar[bool] = False

    def __init__(
        self,
        default_format: str = "msgpack",
        enable_integrity_checking: bool = True,
        use_rust: bool | None = None,  # DEPRECATED
    ):
        """Initialize AutoSerializer.

        Args:
            default_format: Serialization format (currently only "msgpack" supported)
            enable_integrity_checking: Enable ByteStorage for compression and integrity checks (default: True)
                When True: LZ4 compression + xxHash3-64 integrity checking via ByteStorage
                When False: Plain MessagePack (no compression, no integrity checks)
                Note: Setting enable_integrity_checking=False disables integrity checking for @cache.minimal
            use_rust: DEPRECATED - use enable_integrity_checking instead

        Raises:
            ValueError: If default_format is not recognized
        """
        # Handle deprecated use_rust parameter
        if use_rust is not None:
            import warnings

            warnings.warn(
                "use_rust parameter is deprecated, use enable_integrity_checking instead",
                DeprecationWarning,
                stacklevel=2,
            )
            enable_integrity_checking = use_rust

        if default_format not in ("msgpack",):
            raise ValueError(f"Unsupported default_format: '{default_format}'. Supported formats: 'msgpack'")

        self.default_format = default_format
        self.enable_integrity_checking = enable_integrity_checking

        if self.enable_integrity_checking:
            self._byte_storage = ByteStorage(default_format)

        # Initialize ArrowSerializer for fast DataFrame serialization (if available)
        if HAS_ARROW_SERIALIZER:
            self._arrow_serializer = ArrowSerializer()  # type: ignore[misc]
        else:
            self._arrow_serializer = None

        # MessagePack configuration for speed
        self._msgpack_pack_opts = {
            "use_bin_type": True,  # Use bin type for bytes (faster)
            "strict_types": False,  # Allow mixed types (more flexible)
            "default": _auto_default,  # Handle datetime, UUID, set, frozenset
        }
        self._msgpack_unpack_opts = {
            "use_list": True,  # Need lists for DataFrame serialization format
            "raw": False,  # Decode strings properly
            "object_hook": _auto_object_hook,  # Restore datetime, UUID, set, frozenset
        }

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
        """Serialize object to bytes using auto detection.

        Auto-detects:
        - NumPy arrays (efficient binary serialization, if numpy installed)
        - Pandas DataFrames (ArrowSerializer if available, else columnar msgpack)
        - Everything else (MessagePack)

        Args:
            obj: Object to serialize

        Returns:
            Tuple[bytes, SerializationMetadata]: Serialized data with metadata
        """
        # metadata.compressed feeds the AES-GCM AAD v0x03 (EncryptionWrapper binds str(compressed)),
        # so it MUST reflect the codec actually applied: True iff the ByteStorage LZ4 envelope wrapped
        # the payload (parity with StandardSerializer), False for the checksum-only numpy path (#166).
        # Every non-numpy path below envelopes iff integrity checking is on (_serialize_* helpers
        # gate ByteStorage.store on the same flag).
        enveloped = self.enable_integrity_checking

        # NumPy detection (only if numpy installed)
        if HAS_NUMPY and isinstance(obj, np.ndarray):  # type: ignore[union-attr]
            data = self._serialize_numpy(obj)
            metadata = SerializationMetadata(
                serialization_format=SerializationFormat.MSGPACK, compressed=False, original_type="numpy"
            )
            return data, metadata

        # DataFrame detection (delegate to ArrowSerializer if available)
        if HAS_PANDAS and isinstance(obj, pd.DataFrame):  # type: ignore[union-attr]
            if self._arrow_serializer is not None:
                # Use ArrowSerializer for 50-100x faster DataFrame serialization
                # (its metadata already reflects its own configured codec)
                return self._arrow_serializer.serialize(obj)
            # Fallback to msgpack columnar format
            data = self._serialize_dataframe(obj)
            metadata = SerializationMetadata(
                serialization_format=SerializationFormat.MSGPACK,
                compressed=enveloped,
                original_type="dataframe",
            )
            return data, metadata

        # Series detection (only if pandas installed)
        if HAS_PANDAS and isinstance(obj, pd.Series):  # type: ignore[union-attr]
            data = self._serialize_series(obj)
            metadata = SerializationMetadata(
                serialization_format=SerializationFormat.MSGPACK,
                compressed=enveloped,
                original_type="series",
            )
            return data, metadata

        # Default: MessagePack (always available)
        data = self._serialize_msgpack(obj)
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=enveloped,
            original_type="msgpack",
        )
        return data, metadata

    def deserialize(self, data: bytes | memoryview, metadata: Optional[SerializationMetadata] = None) -> Any:
        """Deserialize bytes back to Python object.

        Automatically detects format from envelope and deserializes accordingly.

        Args:
            data: Serialized bytes from serialize()
            metadata: Optional metadata (contains original_type for format detection)

        Returns:
            Any: Deserialized Python object

        Raises:
            SerializationError: A ByteStorage envelope was present but failed verification
                (checksum mismatch, decompression bomb/failure, size mismatch), a verified
                payload failed to decode, or the entry's two records of its own format
                disagree.

                **Verification.** Arrow and ``NUMPY_RAW`` never travel inside a ByteStorage
                envelope, so with integrity checking ON, three routes decide a decode with no
                ``retrieve()`` behind them (with it OFF there are more — see the last paragraph).
                Listed by route, not by direction, because the way this contract keeps going
                wrong is a sweeping claim true of every path its author enumerated:

                * ``[xxh3][ARROW1]`` and ``[xxh3][NUMPY_RAW]`` — routed only when that prefix
                  matches the body's digest (``_checksummed_prefix``). LZ4 emits literals
                  verbatim, so a value that merely CONTAINS either magic lands it at envelope
                  offset 8; the digest check disambiguates a real prefix from that collision.
                  It is not authentication — the digest is unkeyed.
                * bare ``NUMPY_RAW`` at offset 0 — verifies NOTHING. Legacy bare ``[ARROW1]``
                  likewise. Neither can be an envelope (an envelope is an rmp_serde array whose
                  lead byte is never ``N`` or ``A``), so no collision guard is needed.
                * both ``NUMPY_RAW`` routes additionally require the header to AGREE (absent or
                  ``"numpy"``) and RAISE on a disagreement, where the Arrow gate below only skips.
                  Skipping also fails closed — on the envelope gate for an integrity-on reader, on
                  ``unpackb_bounded`` refusing the payload for an integrity-off one — but each
                  names a cause that is not the disagreement, and the cross-config gate cannot
                  rescue the second: it reads ``metadata.compressed``, which is ``False`` for
                  every numpy entry (#166, it feeds the AAD) and for an Arrow entry too whenever
                  ``arrow_compression`` is off. ``compressed=False``, not numpy-vs-Arrow, is the
                  discriminant.
                * a header claiming ``"numpy"`` — reaches the numpy decode on the header's word.

                For every other format, with integrity checking ON, an entry that arrives WITH
                metadata must come from a verified envelope: a ``retrieve()`` failure of any
                kind, including "not an envelope at all", raises rather than reconstructing
                from bytes nothing verified (matching :class:`StandardSerializer`). Apart from
                the three routes above, no field of ``metadata`` re-opens that fall-through —
                the CK header is plaintext, so one flipped byte there must not decide whether
                verification happens.

                An integrity-OFF reader runs no ``retrieve()`` at all, so nothing vouched for its
                decode — **with metadata or without**, not only on the metadata-less call. Both
                entrances close on :meth:`_looks_like_envelope`.

                **That is a deliberately chosen corner, not a solved problem.** Every rule trades,
                measured at each corner:

                ==========================  ========  =========  ============  ===============
                rule                        healthy   rot        map-encoded   envelope-shaped
                ==========================  ========  =========  ============  ===============
                shape (this)                reject    reject \\*  **returned**  **refused**
                shape AND parse             reject    **ret.**   **returned**  accepted
                parse, rot treated as a hit reject    reject     reject        **refused**
                ==========================  ========  =========  ============  ===============

                \\* minus a marker-byte residual, 13 of 12,495 single-byte rots on a 49-byte
                envelope, measured in :meth:`_looks_like_envelope`.

                "Envelope-shaped" is the predicate, not a picture: a top-level 4-element ``list``
                of which any THREE of ``bytes`` / eight small ints / non-negative int / known
                format string hold. Neither the bytes slot nor the format slot is required, so
                ``[b"\x89PNG", [255,0,0,255,0,255,0,255], 4096, "rgb"]`` and
                ``["s", [1,2,3,4,5,6,7,8], 5, "msgpack"]`` are both refused. Only that: a
                ``tuple``, a nested or 3-/5-element list, and every :class:`StandardSerializer`
                read round-trip untouched.

                Returning a rotted envelope hands the caller its compressed payload as their
                object — silent wrong data; refusing an envelope-shaped value is a deterministic
                miss that recomputes the right answer. A clean failure beats a quiet one, so the
                refusal is the cost taken — priced in full: recompute re-produces the same bytes,
                so for that value EVERY read misses, forever, and each raises
                :class:`EnvelopeShapeError`, counted under its own ``envelope_shape`` telemetry
                reason precisely so a permanent benign refusal cannot read as a corruption spike.
                A parse is no escape: a legitimate 4/4 list parses as a ``StorageEnvelope`` and
                then fails the checksum, byte-indistinguishable from a rotted one. Map-encoded
                envelopes (``to_vec_named`` or a foreign writer, never this one and never rot)
                score 0/4 and are returned — this corner's other residual. Closing every cell
                needs the writer to mark an envelope AS one: a wire change (LAB-4304), not a
                read-path rule.

                **Metadata-free reads assume the writer's configuration.** A read with no metadata
                cannot identify the writer, so it is defined only when reader and writer agree on
                ``enable_integrity_checking``. Nothing has to enforce that on the normal path:
                :meth:`CacheKeyGenerator.generate_key` puts that flag in the key suffix, so a
                reconfigured reader misses rather than crossing. It is the
                direct serializer API and a hand-reused raw key that can cross, and there one arm
                has no parse to appeal to — when ``retrieve()`` already failed, a re-parse fails the
                same way, so shape DECIDES and a value shaped >=3/4 like an envelope is refused.
                That residual is the price of not adding a wire discriminator to tell an envelope
                from a list, which would be a format change (LAB-2736), not a read-path fix.

                **Format.** The stored format is recorded twice — in the envelope's ``format``
                field and in the header's ``original_type`` — and the xxHash3-64 covers the
                payload bytes ONLY, so NEITHER copy is verified. Electing either to override
                the other just moves the hole to the other field, so instead: ``format_id``
                must be one :meth:`serialize` can write, and a header claim that is present
                must equal it. A disagreement is corruption -> raise -> evict and recompute.

                A present claim must also BE a string, checked once where this method reads it.
                The header is plaintext JSON and ``SerializationMetadata.from_dict`` does not type
                this slot, so it arrives holding whatever decoded — and a non-str was NOT merely
                an untidy fail-closed. It matches no branch, so an integrity-off ``series`` entry
                whose header rotted to a dict skipped the columnar decode, skipped every gate,
                and returned the envelope body: a ``Series`` came back as a ``dict``, no error
                (measured). Nothing downstream could catch it, because ``x in (tuple)`` compares
                and never hashes — a non-str disagrees with every format without ever being read
                as wrong.

                The message names the TYPE, never the claim: the sites that echo it run ``repr``
                BEFORE ``:.40`` truncates, so an oversized claim is rendered whole to emit 40
                chars, and a deeply nested one raises ``RecursionError`` — not a
                ``SerializationError``, so it escapes past every ``except SerializationError`` a
                direct caller wrote. Typing the slot closes the nesting half outright (only a
                non-str nests) and leaves the size half open to a huge ``str``, which needs the
                backend write access already out of scope above. What keeps the nesting case
                away from a stored entry is that ``json.loads`` in ``SerializationWrapper.unwrap``
                gives out first — but that ordering is one shared C stack, it moves between
                processes of the same interpreter, and for a list the margin is a single frame.
                No number for it is reproducible; the check is here so the ordering need not hold.

                This is corruption and bit-rot containment, NOT an anti-tamper control: the
                checksum is unkeyed, so anyone who can rewrite one field can rewrite both and
                recompute it. Tamper detection needs encryption (E003). See ``E021`` in
                ``docs/error-codes.md``.

                With integrity checking OFF the reader builds no ByteStorage, so no envelope is
                verified — that is what ``@cache.minimal`` chooses — and the envelope-shape
                rejection above applies to its metadata-less reads too, because "nobody ran
                retrieve()" vouches for a decode no more than "retrieve() failed" does; without
                it a HEALTHY envelope came back as its four fields. It is not a blanket "nothing
                is verified": :class:`ArrowSerializer` is constructed regardless of this flag and
                always writes and checks its own checksum, which is why an intact Arrow entry
                whose header lost ``original_type`` still decodes on an integrity-off reader
                rather than failing closed.
        """
        # coerce unwrap's zero-copy memoryview; no-op when already bytes (enables .startswith below + Rust retrieve)
        data = bytes(data)
        # The header's claim about the stored format. Read once: binding it twice under two names
        # is how a header-sourced value reached a variable holding the envelope's format before.
        # Typed once too, here rather than at each of the three sites that compare or echo it —
        # see Raises:. A non-str is not a format claim; without this it merely disagreed with
        # every format by accident, since `in (tuple)` compares and never hashes.
        header_format = getattr(metadata, "original_type", None)
        if header_format is not None and not isinstance(header_format, str):
            raise SerializationError(f"Cache entry header claims a {type(header_format).__name__} format, not a string")

        # Custom NumPy format — bare [NUMPY_RAW...] or checksummed [8-byte xxHash3-64][NUMPY_RAW...].
        # Detected by structure BEFORE the envelope path, even with no metadata. The checksummed
        # arm goes through _checksummed_prefix, the same gate as Arrow below: a bare offset-8
        # magic test handed a checksum-intact ByteStorage entry whose VALUE merely began
        # b"xxNUMPY_RAW" to the numpy decoder, which read the envelope's first 8 bytes as a
        # digest, failed it, and raised on every read — a permanent miss on a healthy key.
        if data.startswith(b"NUMPY_RAW") or self._checksummed_prefix(data, b"NUMPY_RAW"):
            # Agreement, same rule as the envelope below — this route had none, so a "msgpack"
            # header over these bytes still returned an ndarray. Why it raises where the Arrow
            # gate skips: see Raises:, which is the one place that contract is stated.
            if header_format not in (None, "numpy"):
                raise SerializationError(f"NumPy payload disagrees with header format {header_format!r:.40}")
            return self._deserialize_numpy(data)

        # Arrow IPC — [8-byte xxHash3-64][ARROW1...] or bare [ARROW1...]. Detected by structure
        # like the numpy case above and BEFORE the envelope path: Arrow never travels inside a
        # ByteStorage envelope and carries its own checksum, so an entry whose header merely LOST
        # original_type is recoverable here rather than failing closed as an unparseable envelope.
        # A header naming a different format contradicts these bytes, so it is left to the
        # fail-closed gate below instead of being decoded on the strength of either one.
        if header_format in (None, "arrow") and (data[:6] == b"ARROW1" or self._checksummed_prefix(data, b"ARROW1")):
            if self._arrow_serializer is None:
                raise SerializationError(
                    "Cannot deserialize Arrow format: ArrowSerializer not available. Install with: pip install 'cachekit[data]'"
                )
            return self._arrow_serializer.deserialize(data, metadata)

        if metadata is not None:
            # _deserialize_numpy strips + verifies the optional xxHash3-64 checksum prefix itself.
            if header_format == "numpy":
                return self._deserialize_numpy(data)
            # From here down (dataframe, series, generic msgpack) metadata.compressed records whether
            # the writer enveloped the entry — numpy/arrow routed out above, their flag means codec.
            # An integrity-off reader cannot verify or unwrap it: fail closed (see Raises: above).
            # No exemption for header "arrow": excluding it skipped the ONLY raise on this path and an
            # integrity-off reader then handed back an envelope's fields as the value. A corrupt Arrow
            # entry reaching here reports this config message rather than its own checksum failure —
            # that imprecision is the accepted cost of not adding a variation to this gate.
            if metadata.compressed and not self.enable_integrity_checking:
                raise SerializationError(
                    "Cache entry was written with integrity checking on but this reader has "
                    f"integrity checking disabled (format={header_format!r:.40})"
                )
            if header_format in ("dataframe", "series") and not self.enable_integrity_checking:
                # Integrity off: data is direct msgpack (no envelope)
                return self._decode_columnar(data, header_format)
            # Integrity on, every format: the single verified-envelope decode below. The columnar
            # pre-branch that used to sit here decoded on the header alone (LAB-2736 AC6).

        # For Rust-envelope formats, use the Rust layer
        envelope_error: Exception | None = None
        if self.enable_integrity_checking:
            try:
                # Use Rust layer for decompression and validation
                original_data, format_id = self._byte_storage.retrieve(data)
            except EnvelopeIntegrityError as e:
                # The envelope parsed but failed verification (checksum, decompression bomb,
                # size mismatch) — genuine corruption or tampering. Must fail closed, never
                # fall through to a re-parse as plain msgpack/NumPy (that would either raise a
                # confusing "not decodable" error or, worse, decode envelope bytes as if they
                # were the payload).
                raise self._envelope_failure(e) from e
            except Exception as e:
                # Not a ByteStorage envelope at all (e.g. written with integrity checking off):
                # fall through to the Python-only paths below, keeping the reason for the
                # final error.
                envelope_error = e
                logger.debug(
                    f"Rust envelope parsing failed, falling back to Python-only deserialization: {redact_error_for_log(e)}"
                )
            else:
                # The envelope verified (checksum matched), so its payload is exactly what was
                # stored; a payload that then fails to decode is corruption or a forged entry
                # (LAB-2503 decode bomb) and MUST fail closed. Falling through here used to
                # re-decode the ENVELOPE bytes as plain MessagePack and return its positional
                # fields as the cached value — wrong data, silently.
                # Format by agreement, never by a winner — see Raises:.
                if format_id not in _ENVELOPE_FORMATS:
                    raise self._envelope_failure(f"unknown envelope format {format_id!r:.40}")
                if header_format is not None and header_format != format_id:
                    raise self._envelope_failure(
                        f"envelope format {format_id!r:.40} disagrees with header format {header_format!r:.40}"
                    )
                try:
                    if format_id in ("dataframe", "series"):
                        return self._decode_columnar(original_data, format_id)
                    return unpackb_bounded(original_data, **self._msgpack_unpack_opts)
                except PAYLOAD_DECODE_ERRORS as e:
                    raise SerializationError(
                        f"Cache entry payload failed to decode inside a verified envelope (format={format_id!r:.40}): "
                        f"{bounded_error(e)}"
                    ) from e

        # Reached with integrity on when retrieve() raised the "not an envelope" ValueError
        # (envelope_error is set), or with integrity off (envelope_error is None; a
        # compressed=true entry was already rejected above). A call carrying metadata fails
        # closed here — see Raises:. Only the metadata-absent direct-API call falls through.
        if envelope_error is not None and metadata is not None:
            raise self._envelope_failure(envelope_error) from envelope_error

        # Python-only path (no Rust compression) - direct msgpack deserialization
        try:
            value = unpackb_bounded(data, **self._msgpack_unpack_opts)
        except PAYLOAD_DECODE_ERRORS as msgpack_error:
            # NUMPY_RAW entries were routed structurally at the top, so nothing reaching here can be
            # a NumPy payload (and a NumPy attempt would raise RuntimeError without the [data]
            # extra). Report every reason for the miss: the msgpack one is the decode-bound
            # rejection for a forged entry and must not vanish behind the envelope error. Both
            # causes quote untrusted bytes, so both are bounded here.
            raise SerializationError(
                "Cache entry is not a decodable MessagePack payload"
                f"{f' (envelope: {bounded_error(envelope_error)})' if envelope_error else ''}"
                f" (msgpack: {bounded_error(msgpack_error)})"
            ) from msgpack_error
        # A ByteStorage envelope IS valid msgpack — rmp_serde writes StorageEnvelope as
        # [compressed_data, checksum, original_size, format] — so a decode reaching here may be an
        # envelope rather than a value, and returning it hands back the compressed payload as slot
        # 0. Nothing verified this decode: retrieve() failed, or (integrity-off) nobody ran it. One
        # rule for both, and it is shape, because shape is all that is available on EITHER arm — a
        # rotted envelope is exactly what a re-parse cannot confirm. See Raises: for which corner
        # of the trilemma that picks and what it costs.
        if self._looks_like_envelope(value):
            # Its own type and telemetry label: this read cannot tell a rotted envelope from a
            # legitimate 4-element list, so it must not be counted as corruption it cannot prove.
            if envelope_error is not None:
                raise EnvelopeShapeError(
                    "Cache entry failed envelope verification: decoded to a ByteStorage envelope shape after "
                    f"the envelope itself was rejected ({bounded_error(envelope_error)})"
                ) from envelope_error
            raise EnvelopeShapeError(
                "Cache entry failed envelope verification: decoded to a ByteStorage envelope shape that no reader "
                "verified — a rotted envelope, or a value shaped like one; this read cannot tell which (E021)"
            )
        return value

    def _serialize_numpy(self, arr: np.ndarray) -> bytes:  # type: ignore[name-defined]
        """Serialize a NumPy array into the ``NUMPY_RAW`` binary format.

        Requires: numpy installed (HAS_NUMPY=True)

        Raises:
            RuntimeError: If numpy not installed

        When ``enable_integrity_checking`` is on (the default / ``@cache``), an 8-byte
        xxHash3-64 checksum is prepended to the ``NUMPY_RAW`` payload with NO compression,
        mirroring ``ArrowSerializer`` ([checksum][payload]). The numpy branch used to return
        these bytes *unchecked*, so a corrupted entry was reconstructed as silently-wrong data
        on read (#155). Compression is deliberately skipped: numpy is large, often-incompressible
        binary, and LZ4 here measured ~100x slower with no size benefit. When integrity is off
        (``@cache.minimal``), the raw payload is returned without a checksum (msgpack-off parity).
        """
        if not HAS_NUMPY:
            raise RuntimeError("NumPy not installed. Install with: pip install cachekit[data]")

        # Create minimal binary format: [dtype_len][dtype_str][shape_len][shape_data][raw_bytes]
        dtype_str = str(arr.dtype).encode("utf-8")
        dtype_len = len(dtype_str).to_bytes(2, byteorder="little")

        # Encode shape as packed integers
        shape_data = b"".join(dim.to_bytes(4, byteorder="little") for dim in arr.shape)
        shape_len = len(shape_data).to_bytes(2, byteorder="little")

        # Combine: header + raw numpy bytes (zero-copy from NumPy)
        raw = b"NUMPY_RAW" + dtype_len + dtype_str + shape_len + shape_data + arr.tobytes()

        if self.enable_integrity_checking:
            # Checksum-only envelope: prepend the 8-byte xxHash3-64 of the payload, NO compression.
            # numpy arrays are large, often-incompressible binary; routing them through ByteStorage's
            # LZ4 measured ~100x slower to serialize, ~400x slower to read, and inflated incompressible
            # data ~1.56x. This mirrors ArrowSerializer's [8-byte xxHash3-64][payload] scheme, giving
            # the #155 integrity guarantee at ~0 cost. The read side strips + verifies in
            # _deserialize_numpy. (No LZ4 here means numpy is genuinely uncompressed, so the
            # metadata.compressed=False set in serialize() is correct — sidesteps #166 entirely.)
            return xxhash.xxh3_64_digest(raw) + raw
        return raw

    def _deserialize_numpy(self, data: bytes) -> np.ndarray:
        """Deserialize NumPy array from NUMPY_RAW binary format.

        Requires: numpy installed (HAS_NUMPY=True)

        Raises:
            RuntimeError: If numpy not installed
            SerializationError: If data format is invalid or unrecognized
        """
        if not HAS_NUMPY:
            raise RuntimeError("NumPy not installed. Install with: pip install cachekit[data]")

        # Strip + verify the optional 8-byte xxHash3-64 checksum prefix written by integrity-on
        # serialization. Detect by structure (like ArrowSerializer): a checksummed entry is
        # [8-byte checksum][NUMPY_RAW...]; a raw entry (integrity-off / legacy) is [NUMPY_RAW...].
        # A mismatch fails closed (#155) — never reconstructs the corrupted array.
        if not data.startswith(b"NUMPY_RAW") and len(data) >= 17 and data[8:17] == b"NUMPY_RAW":
            if xxhash.xxh3_64_digest(memoryview(data)[8:]) != data[:8]:
                raise SerializationError("NumPy integrity check failed: xxHash3-64 checksum mismatch (corrupted cache entry)")
            data = data[8:]

        if not data.startswith(b"NUMPY_RAW"):
            raise SerializationError("Invalid NumPy data format - expected NUMPY_RAW header")

        try:
            offset = 9  # len(b'NUMPY_RAW')

            # Read dtype
            dtype_len = int.from_bytes(data[offset : offset + 2], byteorder="little")
            offset += 2
            dtype_bytes = data[offset : offset + dtype_len]
            offset += dtype_len

            # Read shape
            shape_len = int.from_bytes(data[offset : offset + 2], byteorder="little")
            offset += 2
            shape_data = data[offset : offset + shape_len]
            offset += shape_len

            # Slicing past the end silently shortens, and a partial 4-byte chunk would parse as a
            # dimension (a forged 1-byte zero chunk = shape (0,) = an empty array instead of an
            # error). Untrusted metadata must be exactly what its length prefix claims.
            if len(dtype_bytes) != dtype_len or len(shape_data) != shape_len or shape_len % 4:
                raise SerializationError("Invalid NumPy data format - truncated or misaligned dtype/shape metadata")
            dtype_str = dtype_bytes.decode("utf-8")

            # Reconstruct shape from packed integers
            shape = []
            for i in range(0, len(shape_data), 4):
                dim = int.from_bytes(shape_data[i : i + 4], byteorder="little")
                shape.append(dim)
            shape = tuple(shape)

            # Extract raw numpy bytes and reconstruct. .copy() so the result is writable and does
            # not alias the source bytes (the L1-cached buffer on a hit) — see #157. frombuffer alone
            # returns a read-only view aliasing the input.
            raw_bytes = data[offset:]
            arr = np.frombuffer(raw_bytes, dtype=_dtype_from_untrusted(dtype_str)).copy()
            return arr.reshape(shape)
        except (ValueError, TypeError, IndexError, SyntaxError) as e:
            # TypeError: np.frombuffer on a forged dtype string; SyntaxError: numpy's comma-string
            # dtype parser runs ast.literal_eval on a forged shape prefix such as "(1,f8";
            # UnicodeDecodeError is a ValueError.
            raise SerializationError(f"Failed to deserialize NumPy array: {bounded_error(e)}") from e

    def _serialize_dataframe(self, df: pd.DataFrame) -> bytes:
        """Serialize DataFrame with column-wise optimization.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed"""
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        # Column-wise serialization
        serialized = {
            "columns": list(df.columns),
            "index": df.index.tolist() if df.index.name or not df.index.equals(pd.RangeIndex(len(df))) else None,
            "data": {},
        }

        # Serialize each column separately — _column_trio is the single writer shared with
        # _serialize_series and mirrored by the _column_values decoder.
        for col in df.columns:
            serialized["data"][col] = _column_trio(df[col])

        msgpack_data = msgpack.packb(serialized, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, "dataframe")  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def _deserialize_dataframe(self, document) -> pd.DataFrame:
        """Rebuild a DataFrame from the already-decoded columnar ``document``.

        ``document`` is the msgpack-decoded body — every production caller (the verified-envelope
        route in :meth:`deserialize` and :meth:`_decode_columnar`) decodes under
        ``unpackb_bounded`` first, so this method never touches the wire bytes and never re-runs
        the decode bound. A forged non-dict body is refused by the ``_expect`` shape gate.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed
            SerializationError: forged document shape — see ``_expect`` / ``_column_values``
        """
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        serialized = _expect(document, dict, "document")
        columns_data = {}
        for col, col_info in _expect(serialized["data"], dict, "data").items():
            what = f"column {col!r:.40}"  # col is attacker-chosen: cap the echo
            columns_data[col] = _column_values(_expect(col_info, dict, what), what)
        df = pd.DataFrame(columns_data, columns=_expect(serialized["columns"], list, "columns"))

        # Restore index if it was serialized
        if serialized["index"] is not None:
            df.index = pd.Index(_expect(serialized["index"], list, "index"))

        return df

    def _serialize_series(self, series: pd.Series) -> bytes:
        """Serialize Pandas Series.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed"""
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        serialized = {
            "name": series.name,
            "index": series.index.tolist() if series.index.name or not series.index.equals(pd.RangeIndex(len(series))) else None,
        }

        # Same {type, data[, dtype]} trio as each DataFrame column, appended after name/index
        # so the on-wire key order is {name, index, type, data[, dtype]} (byte-compatible).
        serialized.update(_column_trio(series))

        msgpack_data = msgpack.packb(serialized, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, "series")  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def _deserialize_series(self, document) -> pd.Series:
        """Rebuild a Series from the already-decoded columnar ``document``.

        ``document`` is the msgpack-decoded body; the same decoded-only contract as
        :meth:`_deserialize_dataframe` (its callers run ``unpackb_bounded`` first). A forged
        non-dict body is refused by the ``_expect`` shape gate.

        Requires: pandas installed (HAS_PANDAS=True)

        Raises:
            RuntimeError: If pandas not installed
            SerializationError: forged document shape — see ``_expect`` / ``_column_values``
        """
        if not HAS_PANDAS:
            raise RuntimeError("Pandas not installed. Install with: pip install cachekit[data]")

        serialized = _expect(document, dict, "document")
        series = pd.Series(_column_values(serialized, "series"), name=serialized["name"])

        # Restore index if it was serialized
        if serialized["index"] is not None:
            series.index = pd.Index(_expect(serialized["index"], list, "index"))

        return series

    @staticmethod
    def _checksummed_prefix(data: bytes, magic: bytes) -> bool:
        """True iff ``data`` is ``[8-byte xxHash3-64][magic...]`` and the digest matches the body.

        One gate for both self-checksummed formats (Arrow, NumPy). It disambiguates a real
        prefix from an LZ4 literal collision — a value that merely CONTAINS ``magic`` lands it at
        envelope offset 8 — and is not authentication: the digest is unkeyed. ``memoryview``
        avoids the full-body copy ``data[8:]`` would make on every read of a large frame.
        """
        end = 8 + len(magic)
        return len(data) > end and data[8:end] == magic and xxhash.xxh3_64_digest(memoryview(data)[8:]) == data[:8]

    @staticmethod
    def _looks_like_envelope(value: object) -> bool:
        """A decoded ``StorageEnvelope`` — ``[bytes, [8 ints], int, format]`` — with at most one
        slot rotted. It decides the whole fall-through, both arms — see ``deserialize``'s Raises:
        for why a parse cannot help here even though it looks like it should. Whichever slot broke
        the parse is exactly the one that may now look wrong; the other
        THREE identify the envelope, and three is exactly what one rot leaves — so requiring all
        four let a single rotted slot through by construction, not by bad luck. Accepting any ONE
        instead false-rejects values a caller legitimately cached: an integrity-off writer's
        ``[1, 2, 3, "msgpack"]`` matched on the format slot alone, a miss recompute reproduces
        forever (LAB-4312).

        Every slot is TYPE-checked before its value is read, so ``in _ENVELOPE_FORMATS`` never
        sees an unhashable slot — there it RAISES ``TypeError`` rather than returning False, and
        that escaped ``deserialize`` uncaught, past every ``except SerializationError`` a caller
        wrote, wherever ``checksum_ok`` did not short-circuit it away first.

        Residual, measured at this head — and NOT the "1 of 391, offset 3, a ``dict``" an
        earlier version claimed, which was an integrity-ON reader's number written into a
        paragraph about the integrity-OFF one. Over ALL 255 substitutions per byte of a
        49-byte envelope, integrity-off reader, no metadata: 13 of 12,495 escape — offset 2
        with ``0x2b``, and offsets 33-36 with ``0xcb``/``0xcf``/``0xd3``, the float64/uint64/
        int64 markers. A marker byte re-partitions every slot after it, so ONE substitution
        damages two slots, the score drops to 2, and ``>= 3`` says "not an envelope". Every
        escape is a ``list``; 12 of the 13 carry the healthy compressed payload byte-identical
        at slot 0. Metadata-present reads: 0 of 12,495, the cross-config gate raises first.
        ``>= 2`` would close all 13 and re-refuse LAB-4312's ``[1, 2, 3, "msgpack"]``, which also
        scores 2 — byte-indistinguishable, so no threshold fixes it (LAB-2736, LAB-4304). A
        crafted entry is out of scope by the same argument: backend write access returns
        arbitrary values through the plain path with no gate involved.
        """
        if not (isinstance(value, list) and len(value) == 4):
            return False
        payload_ok = isinstance(value[0], bytes)
        checksum_ok = (
            isinstance(value[1], list) and len(value[1]) == 8 and all(type(b) is int and 0 <= b <= 255 for b in value[1])
        )
        size_ok = type(value[2]) is int and value[2] >= 0
        format_ok = isinstance(value[3], str) and value[3] in _ENVELOPE_FORMATS
        return sum((payload_ok, checksum_ok, size_ok, format_ok)) >= 3

    @staticmethod
    def _envelope_failure(cause: Exception | str) -> SerializationError:
        """One canonical ``SerializationError`` for every envelope-verification failure in
        ``deserialize`` — see its ``Raises:`` section for the fail-closed contract this backs.

        Takes a plain string for the format checks, whose "cause" is the comparison itself and
        not an exception; constructing a throwaway ``ValueError`` only to stringify it made the
        two call sites read as if something were being wrapped.

        Every cause echoed here is attacker-inflatable, so the bound goes HERE, not per field:
        ``rmp_serde``'s ``DeserializationFailed`` text quotes the envelope slot it choked on, so
        200 KB in the fixed-width ``checksum`` slot walked past three ``!r:.40`` field caps at
        200,162 chars.
        """
        return SerializationError(f"Cache entry failed envelope verification (corrupted cache entry): {bounded_error(cause)}")

    def _decode_columnar(self, payload: bytes | bytearray | memoryview, kind: str) -> pd.DataFrame | pd.Series:
        """Decode a ``dataframe`` / ``series`` payload, failing closed as ``SerializationError``.

        The metadata routes in ``deserialize`` reach here outside the verified-envelope
        normaliser, and the read handler treats only ``SerializationError`` as a read error
        (evict + tamper hook) — a bare ``ValueError`` from the decode bound would be logged as
        a backend fault and the poisoned entry kept (LAB-2503).
        """
        build = self._deserialize_dataframe if kind == "dataframe" else self._deserialize_series
        try:
            return build(unpackb_bounded(payload, **self._msgpack_unpack_opts))
        except PAYLOAD_DECODE_ERRORS as e:
            raise SerializationError(f"Cache entry payload failed to decode as {kind}: {bounded_error(e)}") from e

    def _serialize_msgpack(self, obj: Any) -> bytes:
        """Serialize general object with MessagePack."""
        # Pre-process tuples into markers (msgpack natively flattens them to lists)
        obj = _wrap_tuples(obj)
        msgpack_data = msgpack.packb(obj, **self._msgpack_pack_opts)

        if self.enable_integrity_checking:
            return self._byte_storage.store(msgpack_data, self.default_format)  # type: ignore[return-value]
        else:
            return msgpack_data  # type: ignore[return-value]

    def estimate_compression_ratio(self, obj: Any) -> float:
        """Estimate compression ratio for an object.

        Returns:
            float: Compression ratio (original_size / compressed_size)
        """
        if not self.enable_integrity_checking:
            return 1.0  # Python-only mode has no compression

        # Serialize without compression to get original size
        if HAS_NUMPY and isinstance(obj, np.ndarray):  # type: ignore[union-attr]
            temp_data = msgpack.packb(
                {
                    "data": obj.tobytes(),
                    "shape": obj.shape,
                    "dtype": str(obj.dtype),
                },
                **self._msgpack_pack_opts,
            )
        else:
            temp_data = msgpack.packb(obj, **self._msgpack_pack_opts)

        return self._byte_storage.estimate_compression(temp_data)

    def validate_data(self, data: bytes) -> bool:
        """Validate serialized data without deserializing.

        Args:
            data: Serialized bytes to validate

        Returns:
            bool: True if data is valid and can be deserialized
        """
        if self.enable_integrity_checking:
            return self._byte_storage.validate(data)
        else:
            # Python-only mode validation
            try:
                unpackb_bounded(data, **self._msgpack_unpack_opts)
                return True
            except PAYLOAD_DECODE_ERRORS:
                return False


# Default instance for convenience
auto_serializer = AutoSerializer()
