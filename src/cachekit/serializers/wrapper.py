"""Cache-storage envelope for serialized data.

Wraps serializer output with a small metadata header so cached bytes are
self-describing (serializer name + format flags) without deserializing.
Backend-agnostic: works with Redis, CachekitIO, Memcached, File, L1.

Wire format (v3 binary frame)
-----------------------------
    MAGIC b"CK" | VERSION u8 | HDR_LEN u32-BE | HEADER(json utf-8) | PAYLOAD(raw bytes)
    HEADER = {"s": serializer_name, "m": metadata, "v": envelope_version}

The payload (serializer output: MessagePack/Arrow IPC/ciphertext) is stored
**raw** — no base64, no JSON-embedding. This matters because the previous
base64-in-JSON envelope inflated every binary payload by 1.33x on the wire/in
L1 and forced ~4 full-size copies at peak (b64-bytes -> ascii-str -> json-str ->
utf8-bytes), which made large DataFrames OOM. The frame copies the payload once.

Backward compatibility
-----------------------
`unwrap` reads BOTH formats: a v3 frame (starts with MAGIC b"CK") or the legacy
base64+JSON envelope (a JSON object, starts with b"{" or arrives as str). New
writes always emit the v3 frame; pre-existing cache entries remain readable, so
no cache flush is required (old entries age out by TTL).

This envelope is Python-SDK-internal: backends store it as opaque bytes and the
cross-SDK wire format (ByteStorage MessagePack) is unaffected.
"""

from __future__ import annotations

import base64
import functools
import json
from typing import Any, Union

from cachekit.serializers.base import SerializationMetadata

# v3 binary frame constants
_MAGIC = b"CK"
_FRAME_VERSION = 3
_HEADER_LEN_BYTES = 4  # u32 big-endian header length
_PREFIX_LEN = len(_MAGIC) + 1 + _HEADER_LEN_BYTES  # magic(2) + version(1) + hdrlen(4) = 7

# A process sees a handful of distinct headers (serializer + format flags, plus tenant and key
# fingerprint when encrypted), so every read and write would re-do the same json work. Both are
# memoized, bounded by entry count. Read-side keys are untrusted bytes from the backend: only
# headers that parse and validate are cached, and a header longer than _MEMO_MAX_HEADER_BYTES is
# parsed on every read and never cached, so 256 forged multi-MB headers cannot stay resident and a
# forged entry costs at most an uncached parse. A default header is about 120 bytes; a tenant id
# of a few hundred characters (tenant extractors set no length limit) pushes an encrypted header
# past the cap, and those reads simply skip the memo.
_MEMO_ENTRIES = 256
_MEMO_MAX_HEADER_BYTES = 512
# Header values whose equality implies identical json: the cache key compares by equality, so a
# type with cross-type equals (True == 1, 0.0 == -0.0) would hand one value the other's bytes.
# to_dict emits only these; any other value (int, float, a subclass, a container) is not memoized.
_MEMO_VALUE_TYPES = frozenset({str, bool, type(None)})


def _require_serializer_name(name: Any) -> str:
    """Reject an entry that records no serializer name (protocol: a nameless value is a mismatch).

    Every cachekit-py writer records a name, so its absence means a malformed or tampered entry. Rejecting
    here, in the one parser, keeps it away from every serializer's decode (LAB-4432).
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Cache envelope records no serializer name")
    return name


class _SharedMetadata(SerializationMetadata):
    """A memoized header parse, handed to every read of that header, so it refuses mutation."""

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("memoized SerializationMetadata is shared across reads and read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("memoized SerializationMetadata is shared across reads and read-only")


def _load_header(header: bytes) -> tuple[dict[str, Any], str]:
    """(metadata dict, serializer name) of a v3 frame header: the one validation of its untrusted bytes."""
    parsed = json.loads(header)
    name = _require_serializer_name(parsed.get("s"))
    return parsed.get("m", {}), name


def _parse_header(header: bytes) -> tuple[SerializationMetadata, str]:
    metadata, name = _load_header(header)
    return SerializationMetadata.from_dict(metadata), name


def _with_class(metadata: SerializationMetadata, cls: type[SerializationMetadata]) -> SerializationMetadata:
    """A copy of every attribute as ``cls``, bypassing ``__setattr__`` (which _SharedMetadata refuses)."""
    out = object.__new__(cls)
    out.__dict__.update(vars(metadata))
    return out


@functools.lru_cache(maxsize=_MEMO_ENTRIES)  # never caches a raise: only validated parses are kept
def _parse_header_memo(header: bytes) -> tuple[SerializationMetadata, str]:
    metadata, name = _parse_header(header)
    return _with_class(metadata, _SharedMetadata), name


def _encode_prefix(serializer_name: str, metadata: dict[str, Any], version: str) -> bytes:
    header = json.dumps({"s": serializer_name, "m": metadata, "v": version}, ensure_ascii=False).encode("utf-8")
    return b"".join((_MAGIC, bytes((_FRAME_VERSION,)), len(header).to_bytes(_HEADER_LEN_BYTES, "big"), header))


@functools.lru_cache(maxsize=_MEMO_ENTRIES)
def _encode_prefix_memo(serializer_name: str, items: tuple[tuple[str, Any], ...], version: str) -> bytes:
    return _encode_prefix(serializer_name, dict(items), version)


def _split_frame(wrapped_data: Union[bytes, bytearray, memoryview]) -> tuple[bytes, memoryview] | None:
    """(header bytes, zero-copy payload view) of a v3 frame, or None when it is not one (legacy).

    Raises:
        ValueError: a frame that is truncated, of another frame version, or whose header length overruns it.
    """
    mv = memoryview(wrapped_data)
    if bytes(mv[: len(_MAGIC)]) != _MAGIC:
        return None
    if mv.nbytes < _PREFIX_LEN:
        raise ValueError(f"Truncated cache envelope frame: {mv.nbytes} bytes (minimum {_PREFIX_LEN})")
    frame_version = mv[len(_MAGIC)]
    if frame_version != _FRAME_VERSION:
        raise ValueError(f"Unsupported cache envelope frame version {frame_version} (expected {_FRAME_VERSION})")
    hdr_len = int.from_bytes(mv[len(_MAGIC) + 1 : _PREFIX_LEN], "big")
    header_end = _PREFIX_LEN + hdr_len
    if header_end > mv.nbytes:
        raise ValueError(f"Invalid cache envelope header length {hdr_len}: frame has only {mv.nbytes} bytes")
    # Zero-copy: a memoryview slice past the header aliases the input frame (no full-payload copy
    # on every read). It flows into pa.py_buffer (Arrow) and the mmap read path without
    # materializing. The view keeps `wrapped_data` alive, so it never dangles; consumers needing
    # owned bytes coerce at their own boundary.
    return bytes(mv[_PREFIX_LEN:header_end]), mv[header_end:]


class SerializationWrapper:
    """Frame/unframe serialized bytes with a metadata header for cache storage.

    Examples:
        Wrap and unwrap data:

        >>> data = b"serialized_bytes"
        >>> metadata = {"format": "msgpack", "compressed": True}
        >>> wrapped = SerializationWrapper.wrap(data, metadata, "auto")
        >>> isinstance(wrapped, bytes)
        True

        Unwrap returns original data, metadata, and serializer name:

        >>> unwrapped_data, unwrapped_meta, serializer = SerializationWrapper.unwrap(wrapped)
        >>> unwrapped_data == data
        True
        >>> unwrapped_meta["format"]
        'msgpack'
        >>> serializer
        'auto'

        Binary payloads (non-UTF-8) round-trip without base64:

        >>> raw = bytes(range(256))
        >>> out, _, _ = SerializationWrapper.unwrap(SerializationWrapper.wrap(raw, {}, "default"))
        >>> out == raw
        True
    """

    @staticmethod
    def wrap_prefix(metadata: dict[str, Any], serializer_name: str, version: str = "2.0") -> bytes:
        """Build the v3 frame prefix (everything before the payload): MAGIC | VERSION | HDR_LEN | HEADER.

        The prefix depends only on the metadata/serializer name, never on the payload bytes, so
        the streaming write path (LAB-766) can emit it before a single payload byte exists and
        the stored frame stays byte-identical to a buffered ``wrap`` of the same payload.
        """
        items = tuple(metadata.items())
        if (
            type(serializer_name) is str
            and type(version) is str
            and all(type(k) is str and type(v) in _MEMO_VALUE_TYPES for k, v in items)
        ):
            return _encode_prefix_memo(serializer_name, items, version)
        return _encode_prefix(serializer_name, metadata, version)

    @staticmethod
    def wrap(data: bytes, metadata: dict[str, Any], serializer_name: str, version: str = "2.0") -> bytes:
        """Frame serialized data with a metadata header for cache storage.

        Args:
            data: Serialized bytes to wrap (stored raw — no base64).
            metadata: Serialization metadata dict (must include "format" key).
            serializer_name: Name of serializer used (e.g., "default", "arrow").
            version: Logical serializer-envelope version (carried in the header for
                     downstream compatibility checks; distinct from the binary frame version).

        Returns:
            v3 binary frame bytes: MAGIC | VERSION | HDR_LEN | HEADER(json) | PAYLOAD(raw).
        """
        # Single allocation; the payload is copied exactly once.
        return SerializationWrapper.wrap_prefix(metadata, serializer_name, version) + data

    @staticmethod
    def unwrap(
        wrapped_data: Union[str, bytes, bytearray, memoryview],
    ) -> tuple[Union[bytes, memoryview], dict[str, Any], str]:
        """Unwrap a cache envelope, reading either the v3 frame or the legacy format.

        Args:
            wrapped_data: v3 frame (bytes-like starting with MAGIC) OR legacy base64+JSON
                          envelope (bytes/str starting with '{').

        Returns:
            tuple: (payload, metadata_dict, serializer_name). For a v3 frame the payload is a
            zero-copy ``memoryview`` aliasing ``wrapped_data``; the legacy path returns ``bytes``.

        Raises:
            ValueError: malformed envelope, including one that records no serializer name.
        """
        # v3 binary frame: only bytes-like can be a frame (str is always legacy JSON).
        if isinstance(wrapped_data, (bytes, bytearray, memoryview)):
            frame = _split_frame(wrapped_data)
            if frame is not None:
                header_bytes, payload = frame
                metadata, name = _load_header(header_bytes)
                return payload, metadata, name

        # Legacy base64+JSON envelope (pre-v3 entries; backward compatible read path).
        if isinstance(wrapped_data, (bytes, bytearray, memoryview)):
            wrapped_data = bytes(wrapped_data).decode("utf-8")
        wrapper = json.loads(wrapped_data)
        serializer_name = _require_serializer_name(wrapper.get("serializer"))
        data = base64.b64decode(wrapper["data"].encode("ascii"))
        metadata = wrapper.get("metadata", {})
        return data, metadata, serializer_name

    @staticmethod
    def unwrap_metadata(
        wrapped_data: Union[str, bytes, bytearray, memoryview],
        *,
        shared: bool = True,
    ) -> tuple[Union[bytes, memoryview], SerializationMetadata, str]:
        """:meth:`unwrap` with the header already parsed into :class:`SerializationMetadata`.

        The cache read path's entry point. A v3 frame header of at most ``_MEMO_MAX_HEADER_BYTES``
        is parsed once per distinct header and the result reused, so with ``shared=True`` the returned metadata
        may be shared with other reads and raises AttributeError on assignment. ``shared=False``
        returns a plain copy the caller owns, for code that may write to it (a custom serializer).
        Validation and errors are exactly :meth:`unwrap` followed by ``SerializationMetadata.from_dict``.

        Raises:
            ValueError: as :meth:`unwrap`; also KeyError/TypeError/AttributeError from a header
                whose metadata is malformed, as ``from_dict`` raises them.
        """
        if isinstance(wrapped_data, (bytes, bytearray, memoryview)):
            frame = _split_frame(wrapped_data)
            if frame is not None:
                header_bytes, payload = frame
                if len(header_bytes) > _MEMO_MAX_HEADER_BYTES:
                    metadata, name = _parse_header(header_bytes)
                    return payload, metadata, name
                metadata, name = _parse_header_memo(header_bytes)
                return payload, metadata if shared else _with_class(metadata, SerializationMetadata), name
        payload, metadata_dict, name = SerializationWrapper.unwrap(wrapped_data)
        return payload, SerializationMetadata.from_dict(metadata_dict), name


__all__ = ["SerializationWrapper"]
