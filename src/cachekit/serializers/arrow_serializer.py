# pyright: reportOptionalMemberAccess=false
# pyright: reportInvalidTypeForm=false
"""Apache Arrow IPC serializer for zero-copy DataFrame caching.

This module provides ArrowSerializer for high-performance DataFrame serialization
using Apache Arrow's Inter-Process Communication (IPC) format.

Integrity Protection:
- xxHash3-64 checksums protect against silent data corruption
- Checksum is computed on original Arrow IPC bytes before storage
- Validation occurs during deserialization (detects bit flips, truncation, corruption)
- 8-byte overhead per cached DataFrame (faster than cryptographic hashes)

Optional Dependencies:
- Requires: pip install 'cachekit[data]' (includes pyarrow, pandas)

Type Checking Note:
pandas is guarded at runtime by HAS_PANDAS flag. pyarrow is required at import time
(module fails to load without it, enabling proper import guards in auto_serializer).
Type checker cannot statically verify optional imports; suppressed via pyright config comments above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SerializationError, SerializationFormat, SerializationMetadata

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

# Optional dependency: pandas
try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    pd = None  # type: ignore[assignment]

# Required dependency: pyarrow (fail-fast at module level for import guard in auto_serializer)
try:
    import pyarrow as pa
    import pyarrow.ipc  # noqa: F401 (used via pa.ipc.new_file and pa.ipc.open_file)
except ImportError as e:
    raise ImportError("pyarrow is not installed. ArrowSerializer requires the [data] extra: pip install 'cachekit[data]'") from e

# Standard dependency: xxhash (always available)
import xxhash

# Target bytes per Arrow record-batch when writing IPC. Bounds the zstd compressor's
# working set (compression is per-batch) so peak memory does not scale with table size.
_TARGET_BATCH_BYTES = 8 * 1024 * 1024
# Only return pool memory to the OS for payloads at least this large (avoids churn on
# the small-object hot path, where the syscall + page re-fault would dominate).
_RELEASE_POOL_THRESHOLD = 4 * 1024 * 1024


def _bounded_chunksize(table: pa.Table) -> int | None:  # type: ignore[name-defined]
    """Rows per IPC record-batch so each batch is ~_TARGET_BATCH_BYTES, regardless of width.

    Returns None for empty tables (nothing to chunk). Never returns 0.
    """
    if table.num_rows <= 0:
        return None
    bytes_per_row = max(1, table.nbytes // table.num_rows)
    return max(1, _TARGET_BATCH_BYTES // bytes_per_row)


class ArrowSerializer:
    """Apache Arrow IPC serializer for memory-efficient DataFrame caching with xxHash3-64 integrity.

    Columnar Arrow IPC is far more compact and faster to (de)serialize than MessagePack for
    DataFrames. Supports pandas, polars, and dict of arrays (columnar).
    Does NOT support non-tabular data (scalar values, nested dicts, custom objects).

    Integrity Protection (always on):
    - Format: [8-byte xxHash3-64 checksum][compressed Arrow IPC]
    - Checksum computed over the stored (compressed) IPC bytes
    - Validation on deserialize detects bit flips, truncation, corruption
    - 8-byte overhead per cached DataFrame (negligible vs the payload; never silently
      returns corrupted data, regardless of the enable_integrity_checking flag)

    Memory profile (bounded, low-copy):
    - Serialize: builds the compressed IPC once and prepends the checksum; the source Arrow
      table is freed before the IPC bytes are materialized.
    - Deserialize: the envelope is sliced with a memoryview (no full-body copy), wrapped via
      pa.py_buffer (zero-copy), and Arrow->pandas conversion uses self_destruct + split_blocks
      to free Arrow buffers during conversion. zstd is decompressed transparently by the reader.

    Use cases:
    - Data science pipelines (pandas/polars DataFrames)
    - ML feature stores (model training data caching)
    - Analytics queries (aggregations, filtering on cached DataFrames)
    - Production caching requiring integrity guarantees

    Limitations:
    - DataFrames only (pandas.DataFrame, polars.DataFrame, dict of arrays)
    - NO scalar values (int, str, float)
    - NO nested dicts (must be flattened to columns)
    - NO custom Python objects (unless registered with Arrow extension types)

    Examples:
        >>> serializer = ArrowSerializer()
        >>> df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        >>> data, meta = serializer.serialize(df)
        >>> meta.format
        <SerializationFormat.ARROW: 'arrow'>
        >>> result = serializer.deserialize(data)
        >>> isinstance(result, pd.DataFrame)
        True

        >>> # Unsupported type raises TypeError
        >>> serializer.serialize({"scalar": 123})  # doctest: +SKIP
        Traceback (most recent call last):
        TypeError: ArrowSerializer only supports DataFrames...

        >>> # Corruption detection
        >>> data, _ = serializer.serialize(df)
        >>> corrupted = data[:12] + b'X' + data[13:]  # Corrupt one byte
        >>> serializer.deserialize(corrupted)  # doctest: +SKIP
        Traceback (most recent call last):
        SerializationError: Checksum validation failed - data corruption detected
    """

    def __init__(self, return_format: str = "pandas", enable_integrity_checking: bool = True, compression: str | None = "auto"):
        """Initialize ArrowSerializer.

        Args:
            return_format: Output format for deserialized data ("pandas", "polars", "arrow")
                - "pandas": Convert to pandas.DataFrame (default)
                - "polars": Convert to polars.DataFrame
                - "arrow": Return pyarrow.Table (zero-copy, no conversion)
            enable_integrity_checking: Retained for API compatibility. The 8-byte xxHash3-64
                checksum is now ALWAYS written and validated (silently returning corrupted
                DataFrames is unacceptable, and 8 bytes is negligible), so this flag no longer
                disables integrity.
            compression: Arrow IPC compression codec.
                - "auto" (default): use the CACHEKIT_ARROW_COMPRESSION setting (itself "zstd" by default)
                - "zstd" / "lz4": compress the payload (smaller wire/L1; must be decompressed on read)
                - None or "none": store uncompressed Arrow IPC, enabling zero-copy memory-mapped reads
                  (lowest read memory) at the cost of a larger payload

        Raises:
            ValueError: If return_format or compression is not a valid option
        """
        if return_format not in ("pandas", "polars", "arrow"):
            raise ValueError(f"Invalid return_format: '{return_format}'. Valid options: 'pandas', 'polars', 'arrow'")
        self.return_format = return_format
        self.enable_integrity_checking = enable_integrity_checking
        self.compression = self._resolve_compression(compression)

    @staticmethod
    def _resolve_compression(compression: str | None) -> str | None:
        """Normalize/validate the compression option. 'auto' resolves from settings."""
        if compression == "auto":
            try:
                from cachekit.config.singleton import get_settings

                compression = get_settings().arrow_compression
            except Exception:  # noqa: BLE001 — settings unavailable: fall back to a sane default
                compression = "zstd"
        if compression in (None, "none"):
            return None
        if compression not in ("zstd", "lz4"):
            raise ValueError(f"Invalid compression: {compression!r}. Valid options: 'auto', 'zstd', 'lz4', None ('none').")
        return compression

    def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:  # type: ignore[name-defined]
        """Serialize DataFrame to Arrow IPC format bytes with optional xxHash3-64 integrity protection.

        Args:
            obj: DataFrame (pandas, polars) or dict of arrays (columnar)

        Returns:
            Tuple of (Arrow IPC bytes, metadata)
            Format (integrity ON): [8-byte xxHash3-64 checksum][Arrow IPC bytes]
            Format (integrity OFF): [Arrow IPC bytes]

        Raises:
            TypeError: If obj is not a DataFrame or dict of arrays
            SerializationError: If Arrow conversion fails
        """
        try:
            # Convert to Arrow Table (supports pandas, polars, dict of arrays).
            # preserve_index=None (pyarrow default): a RangeIndex is stored as cheap
            # schema metadata (no materialized column / extra copy) and restored as a
            # RangeIndex; named/MultiIndex are still preserved as columns. preserve_index=True
            # would force even a RangeIndex into a materialized column.
            table = None
            if HAS_PANDAS and isinstance(obj, pd.DataFrame):
                table = pa.Table.from_pandas(obj, preserve_index=None)
            elif hasattr(obj, "__arrow_c_stream__"):  # polars DataFrame (zero-copy C Stream)
                table = pa.table(obj)
            elif isinstance(obj, dict):
                # dict of arrays (columnar). Normalize pyarrow's raw conversion errors
                # (e.g. dict-of-scalars -> "'int' object is not iterable") into the
                # documented TypeError so callers get a consistent, actionable message.
                try:
                    table = pa.table(obj)
                except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as e:
                    raise TypeError(
                        f"ArrowSerializer only supports DataFrames "
                        f"(pandas.DataFrame, polars.DataFrame) or dict of arrays (columnar). "
                        f"Got a dict that is not convertible to an Arrow table: {e}. "
                        f"For scalar values or nested dicts, use AutoSerializer."
                    ) from e

            if table is None:
                raise TypeError(
                    f"ArrowSerializer only supports DataFrames "
                    f"(pandas.DataFrame, polars.DataFrame) or dict of arrays. "
                    f"Got: {type(obj).__name__}. "
                    f"For scalar values or nested dicts, use AutoSerializer."
                )

            # Serialize to Arrow IPC. Compression (when enabled) runs per record-batch, so
            # writing in bounded batches keeps the compressor's working set bounded (one big
            # batch makes the codec allocate a full-size working buffer — measured ~3.6x the
            # payload). Size each batch to ~8 MiB regardless of schema width. compression=None
            # writes uncompressed IPC, which a reader can memory-map zero-copy.
            max_chunksize = _bounded_chunksize(table)
            sink = pa.BufferOutputStream()
            write_options = pa.ipc.IpcWriteOptions(compression=self.compression) if self.compression else None
            with pa.ipc.new_file(sink, table.schema, options=write_options) as writer:
                writer.write_table(table, max_chunksize=max_chunksize)
            del table  # free the Arrow table before materializing the IPC bytes (lowers peak)

            # Always integrity-protect: hash over the buffer's memoryview (no copy), then
            # build the [8-byte xxHash3-64 checksum][compressed Arrow IPC] envelope. The
            # checksum is unconditional — silently returning corrupted DataFrames is
            # unacceptable, and 8 bytes is negligible against the payload.
            buf = sink.getvalue()
            checksum = xxhash.xxh3_64_digest(memoryview(buf))
            envelope = checksum + buf.to_pybytes()

            # For large payloads, return the compressor/buffer working memory the Arrow pool
            # retained back to the OS so it does not stack under the caller's next allocation
            # (the envelope wrap). No-op cost is trivial; gated to avoid churn on small objects.
            if len(envelope) >= _RELEASE_POOL_THRESHOLD:
                del buf
                pa.default_memory_pool().release_unused()

            return envelope, SerializationMetadata(
                serialization_format=SerializationFormat.ARROW,
                compressed=self.compression is not None,  # reflects the configured codec (None = uncompressed)
                encrypted=False,  # Encryption is EncryptionWrapper's responsibility
                original_type="arrow",
            )
        except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError) as e:
            raise SerializationError(f"Failed to serialize DataFrame to Arrow IPC format: {e}") from e

    def deserialize(self, data: bytes, metadata: SerializationMetadata | None = None) -> Any:
        """Deserialize Arrow IPC bytes with optional xxHash3-64 integrity validation.

        Args:
            data: Bytes from serialize() (with or without checksum envelope)
            metadata: Optional metadata (ignored - Arrow IPC is self-describing)

        Returns:
            Deserialized DataFrame (format depends on return_format setting)

        Raises:
            SerializationError: If data is malformed, Arrow deserialization fails, or checksum validation fails
        """
        try:
            # Detect the envelope by sniffing the Arrow IPC file magic (b"ARROW1") rather
            # than trusting an integrity flag — this auto-handles checksummed, raw (legacy
            # integrity-off), and version-mismatch data, and never feeds a checksum prefix
            # into the IPC reader (which previously leaked a bare OSError). memoryview slicing
            # avoids the full-body copy that `data[8:]` used to make.
            mv = memoryview(data)
            n = mv.nbytes
            if n >= 14 and bytes(mv[8:14]) == b"ARROW1":
                # [8-byte xxHash3-64 checksum][Arrow IPC]
                expected_checksum = bytes(mv[:8])
                body = mv[8:]
                if xxhash.xxh3_64_digest(body) != expected_checksum:
                    raise SerializationError("Checksum validation failed - data corruption detected")
            elif n >= 6 and bytes(mv[:6]) == b"ARROW1":
                # Legacy raw Arrow IPC written without a checksum prefix (integrity-off entry)
                body = mv
            else:
                raise SerializationError(
                    f"Invalid data: not a recognized Arrow envelope "
                    f"(expected [8-byte checksum][Arrow IPC] or raw Arrow IPC); got {n} bytes"
                )

            # pa.py_buffer over the memoryview is zero-copy; open_file decompresses transparently.
            reader = pa.ipc.open_file(pa.py_buffer(body))
            table = reader.read_all()

            # Convert to requested format
            if self.return_format == "pandas":
                # self_destruct frees each Arrow column as it is converted (the table is a
                # throwaway local here, so the experimental-invalidation caveat does not apply);
                # split_blocks avoids the transient 2x of consolidated-block construction.
                return table.to_pandas(self_destruct=True, split_blocks=True)
            elif self.return_format == "polars":
                # Import polars only if needed (avoid mandatory dependency)
                try:
                    import polars as pl  # type: ignore[import-not-found]

                    return pl.from_arrow(table)
                except ImportError as import_err:
                    raise SerializationError("polars not installed. Install with: pip install polars") from import_err
            else:  # return_format == "arrow"
                return table  # zero-copy, no conversion

        except (pa.ArrowInvalid, pa.ArrowSerializationError, OSError) as e:
            raise SerializationError(f"Failed to deserialize Arrow IPC data: {e}") from e
