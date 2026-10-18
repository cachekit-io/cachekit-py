**[Home](../README.md)** › **[Serializers](README.md)** › **ArrowSerializer**

# ArrowSerializer

**DataFrame-optimized serializer** — Columnar serialization for pandas and polars DataFrames using the Apache Arrow IPC format, zstd-compressed by default.

## Overview

**Best for:**
- pandas DataFrames, any size
- polars DataFrames
- Data science workloads
- Time-series data
- High-frequency DataFrame caching

**Performance characteristics:**
- Payload: Arrow IPC, compressed with zstd by default (`CACHEKIT_ARROW_COMPRESSION`), so a read decompresses it
- Zero-copy reads: only for uncompressed, plaintext payloads on the File backend; what stays zero-copy depends on `return_format` and the Python version (see [Zero-Copy Reads on the File Backend](#zero-copy-reads-on-the-file-backend)). The stored payload is then larger
- Network overhead: Efficient columnar format

StandardSerializer (MessagePack) does not accept DataFrames, so there is no MessagePack figure to compare against. Sizes and instruction counts measured on a real dataset are in [Performance on Real Data](#performance-on-real-data).

## Basic Usage

```python
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

@cache(serializer=ArrowSerializer())
def load_stock_data(symbol: str):
    # Returns large DataFrame
    return fetch_historical_prices(symbol)  # doctest: +SKIP
```

**Basic example:**

```python notest
from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import ArrowSerializer
import pandas as pd

# Explicit ArrowSerializer for DataFrame caching. backend=None would keep only the L1
# in-memory copy, which stores the DataFrame object itself and never runs the serializer.
backend = FileBackend(FileBackendConfig(cache_dir="/tmp/cachekit"))

@cache(serializer=ArrowSerializer(), backend=backend)
def get_large_dataset(date: str):
    # Load 100K+ row DataFrame (illustrative - file may not exist)
    df = pd.read_csv(f"data/{date}.csv")
    return df

# Automatic round-trip with pandas DataFrame
df = get_large_dataset("2024-01-01")  # Cache miss: loads CSV
df = get_large_dataset("2024-01-01")  # Cache hit: fast retrieval (~1ms)
```

## Return Format Options

ArrowSerializer supports multiple return formats for deserialization:

```python
from cachekit.serializers import ArrowSerializer

# Return as pandas DataFrame (default)
serializer = ArrowSerializer(return_format="pandas")

# Return as polars DataFrame (requires polars installed)
serializer = ArrowSerializer(return_format="polars")

# Return as pyarrow.Table (no conversion; zero-copy on an uncompressed File-backend hit on Python 3.13+)
serializer = ArrowSerializer(return_format="arrow")
```

A pandas result accepts in-place edits after a backend hit (`df.loc[0, "a"] = 1`), exactly as the uncached result does: no column is a read-only view of the cached bytes. A frame that fits one record batch (about 8 MiB) costs one extra copy of its numeric, datetime and categorical columns for this; larger frames do not.

With an uncompressed payload on the File backend (`ArrowSerializer(compression=None)`, or `CACHEKIT_ARROW_COMPRESSION=none` for `serializer="arrow"`), a hit reads the stored file through a memory map. Parts of the result that pandas never edits in place can still reference that map: the categories of a numeric or datetime categorical, a `CategoricalIndex`, and Arrow-backed columns such as pandas 3's default strings. The map, and the disk space of a cache file deleted in the meantime, is released when the result is. Before Python 3.13 the map also holds a file descriptor until then (see [Zero-Copy Reads on the File Backend](#zero-copy-reads-on-the-file-backend)).

**Example with polars:**
```python notest
import polars as pl
from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import ArrowSerializer

backend = FileBackend(FileBackendConfig(cache_dir="/tmp/cachekit"))

@cache(serializer=ArrowSerializer(return_format="polars"), backend=backend)
def get_polars_data():
    return pl.DataFrame({
        "id": [1, 2, 3],
        "value": [10.5, 20.3, 30.1]
    })
```

## Supported Data Types

ArrowSerializer supports:
- `pandas.DataFrame` (with index preservation)
- `polars.DataFrame` (via `__arrow_c_stream__` interface)
- `dict` of arrays (converted to DataFrame)

**Not supported.** Calling `serialize()` directly raises `TypeError` for these. A `@cache`-decorated call with a backend does not raise: it returns the value, caches nothing, and logs the failure ([Troubleshooting → Serialization Failures](../troubleshooting.md#common-errors)).
- Scalar values (int, str, float)
- Lists of objects
- Dicts with a number or bool value (`{"id": 1}`)
- Dicts with a string, bytes or dict value (`{"name": "Alice"}`, `{"user": {"name": "x"}}`)

Every dict value must be a column Arrow can convert: a list, tuple, NumPy array, pandas Series, pyarrow array or typed `memoryview`, or any object that implements the Arrow array protocol (`__arrow_array__` or `__arrow_c_array__`), a `Mapping` included. A `memoryview` is read as its element type, so a memoryview of `bytes` stores byte values. Flatten nested dicts into columns first, or use [AutoSerializer](./auto.md).

**Type checking example:**
```python
from cachekit.serializers import ArrowSerializer

serializer = ArrowSerializer()

# Works: DataFrame
df = pd.DataFrame({"a": [1, 2, 3]})
data, meta = serializer.serialize(df)

# Raises TypeError with helpful message
try:
    serializer.serialize({"key": "value"})
except TypeError as e:
    print(e)
    # "... Got a dict that is not convertible to an Arrow table: value for 'key' is str, not a list or array. ..."
```

## Performance on Real Data

Measured on a public dataset: one month of the USGS earthquake catalogue (January 2024), 12,535 rows by 13 columns, 3.9 MB in memory. It mixes timezone-aware timestamps, a nullable `Int64`, categoricals and high-cardinality strings. The slice and its provenance are in the repository at `tests/data/`. `tests/unit/test_real_dataset.py` asserts on every pull request that each size stays at most 10% above the figure below, and that every round trip is exact (dtypes, index and names).

| What is cached | Shape | Encoded bytes |
|----------------|-------|--------------:|
| The full frame | 12,535 × 13 | 673,906 |
| A filtered, sorted frame indexed by event id | 2,249 × 12 | 118,938 |
| A `groupby` with named aggregations | 15 × 4 | 4,026 |
| A `pivot_table` of mean magnitude | 15 × 6 | 4,890 |

A full-frame round trip (`serialize` then `deserialize`) costs about 60 million instructions on the calling thread, on CPython 3.12.12 and 3.14.3, x86_64 Linux (`make perf-ir`, paths `serializer_arrow_usgs` and `serializer_auto_usgs`; pyarrow runs part of the work on its own threads, which this figure leaves out). A 100-row frame costs about 2 million, so a small frame pays mostly fixed per-call cost. These are instruction counts, not wall time: see [Instruction Budgets](../performance.md#instruction-budgets).

`serializer="auto"` hands a DataFrame to ArrowSerializer when pyarrow is installed, so it produces the same bytes.

For comprehensive performance analysis including decorator overhead, concurrent access, and encryption impact, see [Performance Guide](../performance.md).

### Memory Usage

A read of a compressed payload (the default) decompresses it into new buffers. An uncompressed, plaintext payload on the File backend can be read without copying it, depending on `return_format` and the Python version, at the cost of a larger stored payload: see [Zero-Copy Reads on the File Backend](#zero-copy-reads-on-the-file-backend). Wire backends always copy the payload in.

**Writes stream on the File backend.** When the cache backend supports streaming writes
(File backend only today) and the value is plaintext (no encryption), serialization streams
~8 MiB record batches directly into the cache file instead of materializing the whole Arrow
IPC payload in memory first — cutting write peak RSS from ~5.6x to ~2.3x the DataFrame's
logical size. Wire backends (Redis, CachekitIO, Memcached) and encrypted values use the
buffered path unchanged. See [File Backend](../backends/file.md#bounded-memory-large-values-arrow).

### Zero-Copy Reads on the File Backend

A cache hit memory-maps the cache file instead of reading it into memory when all of these hold:

- the backend is a `FileBackend` on a POSIX system, and the stored file is at most 512 MB;
- the serializer is cachekit's `ArrowSerializer`, as `serializer="arrow"` or an `ArrowSerializer(...)` instance (a subclass reads the buffered way);
- the value is not encrypted, and the cached function is synchronous (hits of an `async def` function read the file in);
- `return_format="pandas"`, or Python 3.13 or later for `"arrow"` and `"polars"`. Arrow and polars results keep the mapping open, and before 3.13 every open mapping also holds a file descriptor, so a few hundred held results would exhaust the process's descriptor limit. On earlier Pythons they read the file in instead. A pandas result keeps the mapping, and before 3.13 its descriptor, only through a column that stays a view of the mapped data.

A compressed payload (the default) is then decompressed out of the mapping into new memory. Only an uncompressed payload, `ArrowSerializer(compression=None)` or `CACHEKIT_ARROW_COMPRESSION=none` for `serializer="arrow"`, can stay zero-copy, and what an uncompressed hit costs depends on `return_format`:

| `return_format` | Uncompressed File-backend hit |
|---|---|
| `"arrow"` | Zero-copy. Every buffer of the returned `pyarrow.Table` points into the mapped cache file, and the hit allocates no heap memory for the data. |
| `"polars"` | Numeric, boolean and temporal columns are zero-copy. String columns are converted to polars' own layout, which copies them. The frame keeps one chunk per stored record batch (about 8 MiB each); call `.rechunk()` if you need contiguous columns. |
| `"pandas"` | One copy: the result is an ordinary, writable pandas DataFrame. |

A zero-copy result keeps the mapping alive for as long as you hold it, and it is unmapped when the result is garbage-collected. Deleting, overwriting or evicting the entry meanwhile does not change the result: cachekit replaces a cache file by renaming a new one over it and deletes by unlinking, so the held result keeps reading the old data. The disk space of a replaced or deleted entry is reclaimed only after the last result that maps it is dropped. Do not truncate or rewrite cache files in place from outside cachekit while results are held: touching a truncated mapping raises `SIGBUS`.

Uncompressed entries are written with their Arrow data starting on a 64-byte boundary of the cache file (64 bytes is Arrow's recommended alignment), so polars imports fixed-width columns without copying. Entries written by cachekit-py releases before this alignment still read correctly; polars copies their numeric columns.

```python
import tempfile

import pandas as pd
import pyarrow as pa
from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import ArrowSerializer

backend = FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp()))

@cache(serializer=ArrowSerializer(return_format="arrow", compression=None), backend=backend, l1_enabled=False)
def load_prices():
    return pd.DataFrame({"price": [float(i) for i in range(100_000)]})

load_prices()  # miss: streams the uncompressed Arrow IPC into the cache file
pool_before = pa.total_allocated_bytes()
table = load_prices()  # hit: a pyarrow.Table over the mapped file on Python 3.13+ (read in before 3.13)
assert table.num_rows == 100_000
assert pa.total_allocated_bytes() == pool_before  # no Arrow buffers were allocated for the hit
```

## Polars Support

Polars DataFrames are written through the `__arrow_c_stream__` interface (Arrow C Data Interface), with no intermediate pandas conversion. Reads with `return_format="polars"` of an uncompressed payload keep one chunk per stored record batch, so the columns can stay views of the stored bytes; a compressed payload is decompressed into new memory anyway and comes back with contiguous columns. See [Zero-Copy Reads on the File Backend](#zero-copy-reads-on-the-file-backend) for which columns stay zero-copy.

```python notest
import polars as pl
from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import ArrowSerializer

backend = FileBackend(FileBackendConfig(cache_dir="/tmp/cachekit"))

@cache(serializer=ArrowSerializer(return_format="polars", compression=None), backend=backend)
def get_polars_data():
    return pl.DataFrame({
        "id": [1, 2, 3],
        "value": [10.5, 20.3, 30.1]
    })
```

**Polars requires `polars` to be installed:**
```bash
pip install polars
# or
uv add polars
```

If polars is not installed, deserializing with `return_format="polars"` raises `SerializationError` ("polars not installed"). Under `@cache`, every hit then logs that error and runs the function again, as for a corrupt entry.

## Performance Optimization Tips

1. **Use return_format="arrow"** to skip the pandas conversion, and add `compression=None` on the File backend for zero-copy hits on Python 3.13+ ([details](#zero-copy-reads-on-the-file-backend)):

   ```python notest
   from cachekit import cache
   from cachekit.backends.file import FileBackend, FileBackendConfig
   from cachekit.serializers import ArrowSerializer

   backend = FileBackend(FileBackendConfig(cache_dir="/tmp/cachekit"))

   @cache(serializer=ArrowSerializer(return_format="arrow", compression=None), backend=backend)
   def get_data():
       return df  # illustrative - df not defined

   # Result is a pyarrow.Table (no pandas conversion); on Python 3.13+ it is a view of the mapped cache file, no copy
   table = get_data()
   ```

2. **Preserve pandas index** for efficient round-trips:

   ```python
   # ArrowSerializer automatically preserves pandas index
   df = pd.DataFrame({"a": [1, 2, 3]}, index=pd.Index([10, 20, 30], name="id"))
   # Index is preserved through serialization/deserialization
   ```

3. **Batch similar queries** to amortize cache lookup overhead:

   ```python notest
   from cachekit import cache
   from cachekit.serializers import ArrowSerializer
   import pandas as pd

   @cache(serializer=ArrowSerializer(), backend=backend)  # backend as in tip 1
   def get_data_batch(date_range):
       # Return one large DataFrame instead of many small ones
       return pd.concat([load_day(d) for d in date_range])  # illustrative - load_day not defined
   ```

---

## See Also

- [StandardSerializer](default.md) — Better choice for DataFrames under 1K rows
- [OrjsonSerializer](orjson.md) — JSON-optimized for API data
- [Encryption Wrapper](encryption.md) — Add zero-knowledge encryption to ArrowSerializer
- [Performance Guide](../performance.md) — Full benchmark comparisons
- [Troubleshooting Guide](../troubleshooting.md) — Serialization error solutions

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
