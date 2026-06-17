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
import json
from typing import Any, Union

# v3 binary frame constants
_MAGIC = b"CK"
_FRAME_VERSION = 3
_HEADER_LEN_BYTES = 4  # u32 big-endian header length
_PREFIX_LEN = len(_MAGIC) + 1 + _HEADER_LEN_BYTES  # magic(2) + version(1) + hdrlen(4) = 7


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
        header = json.dumps(
            {"s": serializer_name, "m": metadata, "v": version},
            ensure_ascii=False,
        ).encode("utf-8")
        # Single allocation; the payload is copied exactly once.
        return b"".join(
            (
                _MAGIC,
                bytes((_FRAME_VERSION,)),
                len(header).to_bytes(_HEADER_LEN_BYTES, "big"),
                header,
                data,
            )
        )

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
        """
        # v3 binary frame: only bytes-like can be a frame (str is always legacy JSON).
        if isinstance(wrapped_data, (bytes, bytearray, memoryview)):
            mv = memoryview(wrapped_data)
            if bytes(mv[: len(_MAGIC)]) == _MAGIC:
                if mv.nbytes < _PREFIX_LEN:
                    raise ValueError(f"Truncated cache envelope frame: {mv.nbytes} bytes (minimum {_PREFIX_LEN})")
                frame_version = mv[len(_MAGIC)]
                if frame_version != _FRAME_VERSION:
                    raise ValueError(f"Unsupported cache envelope frame version {frame_version} (expected {_FRAME_VERSION})")
                hdr_len = int.from_bytes(mv[len(_MAGIC) + 1 : _PREFIX_LEN], "big")
                header_end = _PREFIX_LEN + hdr_len
                if header_end > mv.nbytes:
                    raise ValueError(f"Invalid cache envelope header length {hdr_len}: frame has only {mv.nbytes} bytes")
                header = json.loads(bytes(mv[_PREFIX_LEN:header_end]))
                # Zero-copy: a memoryview slice past the header aliases the input frame (no
                # full-payload copy on every read). It flows into pa.py_buffer (Arrow) and the
                # mmap read path without materializing. The view keeps `wrapped_data` alive, so
                # it never dangles; consumers needing owned bytes coerce at their own boundary.
                payload = mv[header_end:]
                return payload, header.get("m", {}), header.get("s", "unknown")

        # Legacy base64+JSON envelope (pre-v3 entries; backward compatible read path).
        if isinstance(wrapped_data, (bytes, bytearray, memoryview)):
            wrapped_data = bytes(wrapped_data).decode("utf-8")
        wrapper = json.loads(wrapped_data)
        data = base64.b64decode(wrapper["data"].encode("ascii"))
        metadata = wrapper.get("metadata", {})
        serializer_name = wrapper.get("serializer", "unknown")
        return data, metadata, serializer_name


__all__ = ["SerializationWrapper"]
