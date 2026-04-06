**[Home](../README.md)** › **[Serializers](README.md)** › **ArrowSerializer**

# ArrowSerializer

**DataFrame-optimized serializer** — Zero-copy serialization for pandas and polars DataFrames using Apache Arrow IPC format.

## Overview

**Best for:**
- Large pandas DataFrames (10K+ rows)
- Large polars DataFrames
- Data science workloads
- Time-series data
- High-frequency DataFrame caching

**Performance characteristics:**
- Serialization: **3-6x faster** than MessagePack for large DataFrames
- Deserialization: **7-20x faster** (memory-mapped, zero-copy)
- Memory overhead: Minimal (zero-copy deserialization)
- Network overhead: Efficient columnar format

**Measured speedups:**
- **10K rows**: 0.80ms (Arrow) vs 3.96ms (MessagePack) = **5.0x faster**
- **100K rows**: 4.06ms (Arrow) vs 39.04ms (MessagePack) = **9.6x faster**

For detailed performance analysis, see [Performance Guide](../performance.md).

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

**Not supported:**
- Scalar values (int, str, float) → raises `TypeError`
- Nested dictionaries → raises `TypeError`
- Lists of objects → raises `TypeError`

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
    # "ArrowSerializer only supports DataFrames. Use StandardSerializer for dict types."
```

## Performance Benchmarks

Real-world performance benchmarks (measured on M1 Mac):

**Serialization (encode to bytes):**
| DataFrame Size | Arrow Time | Default Time | Speedup |
|----------------|------------|--------------|---------|
| 1K rows | 0.29ms | 0.20ms | 0.7x (overhead for small data) |
| 10K rows | 0.48ms | 1.64ms | **3.4x** |
| 100K rows | 2.93ms | 16.42ms | **5.6x** |

**Deserialization (decode from bytes):**
| DataFrame Size | Arrow Time | Default Time | Speedup |
|----------------|------------|--------------|---------|
| 1K rows | 0.21ms | 0.39ms | **1.8x** |
| 10K rows | 0.32ms | 2.32ms | **7.1x** |
| 100K rows | 1.13ms | 22.62ms | **20.1x** |

**Total Roundtrip (serialize + deserialize):**
| DataFrame Size | Arrow Total | Default Total | Speedup |
|----------------|-------------|---------------|---------|
| 10K rows | 0.80ms | 3.96ms | **5.0x** |
| 100K rows | 4.06ms | 39.04ms | **9.6x** |

> [!NOTE]
> ArrowSerializer shines for DataFrames with 10K+ rows. For smaller data (< 1K rows), StandardSerializer has lower overhead.

For comprehensive performance analysis including decorator overhead, concurrent access, and encryption impact, see [Performance Guide](../performance.md).

### Memory Usage

ArrowSerializer uses memory-mapped deserialization, which means:
- No full copy of data into memory
- Minimal memory allocation
- Faster garbage collection

**Example comparison (100K rows):**
- Default deserialization: +15 MB memory allocation
- Arrow deserialization: +2 MB memory allocation

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
