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
- Zero-copy reads: only with `ArrowSerializer(compression=None)` on the File backend, where a plaintext read memory-maps the stored payload; the stored payload is then larger
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
from cachekit.serializers import ArrowSerializer
import pandas as pd

# Explicit ArrowSerializer for DataFrame caching
@cache(serializer=ArrowSerializer(), backend=None)
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

# Return as pyarrow.Table (zero-copy, fastest)
serializer = ArrowSerializer(return_format="arrow")
```

**Example with polars:**
```python notest
import polars as pl
from cachekit import cache
from cachekit.serializers import ArrowSerializer

@cache(serializer=ArrowSerializer(return_format="polars"), backend=None)
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

Measured on a public dataset: one month of the USGS earthquake catalogue (January 2024), 12,535 rows by 13 columns, 3.9 MB in memory. It mixes timezone-aware timestamps, a nullable `Int64`, categoricals and high-cardinality strings. The slice and its provenance are in the repository at `tests/data/`. `tests/unit/test_real_dataset.py` asserts each size below within 10% on every pull request, and that every round trip is exact (dtypes, index and names).

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

A read of a compressed payload (the default) decompresses it into new buffers. With `ArrowSerializer(compression=None)` on the File backend, a plaintext read memory-maps the stored file instead of copying it, at the cost of a larger stored payload. Wire backends always copy the payload in.

**Writes stream on the File backend.** When the cache backend supports streaming writes
(File backend only today) and the value is plaintext (no encryption), serialization streams
~8 MiB record batches directly into the cache file instead of materializing the whole Arrow
IPC payload in memory first — cutting write peak RSS from ~5.6x to ~2.3x the DataFrame's
logical size. Wire backends (Redis, CachekitIO, Memcached) and encrypted values use the
buffered path unchanged. See [File Backend](../backends/file.md#bounded-memory-large-values-arrow).

## Polars Support

Polars DataFrames are supported via the `__arrow_c_stream__` interface (Arrow C Data Interface). This means zero-copy interchange between polars and Arrow — no intermediate conversion.

```python notest
import polars as pl
from cachekit import cache
from cachekit.serializers import ArrowSerializer

@cache(serializer=ArrowSerializer(return_format="polars"), backend=None)
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

If polars is not installed and `return_format="polars"` is specified, an `ImportError` is raised.

## Performance Optimization Tips

1. **Use return_format="arrow"** for zero-copy access:

   ```python notest
   from cachekit import cache
   from cachekit.serializers import ArrowSerializer

   @cache(serializer=ArrowSerializer(return_format="arrow"), backend=None)
   def get_data():
       return df  # illustrative - df not defined

   # Result is pyarrow.Table (no pandas conversion overhead)
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

   @cache(serializer=ArrowSerializer(), backend=None)
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
