# Real-world test data

## `usgs_earthquakes_2024-01.parquet`

Every earthquake and other seismic event in the USGS Comprehensive Catalog (ComCat) for January 2024:
12,535 rows, 13 columns, 598 KiB. `tests/unit/test_real_dataset.py` round-trips it, and four data-science
results derived from it, through the DataFrame serializers and the decorator's backend path.
`tests/performance/ir_workload.py` measures the `serializer_*_usgs` instruction-count paths on it.

| Field | Value |
|---|---|
| Source | <https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime=2024-01-01&endtime=2024-02-01&orderby=time-asc> |
| Retrieved | 2026-10-08 |
| Licence | U.S. public domain: "USGS-authored or produced data and information are considered to be in the U.S. Public Domain" (<https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits>) |
| Credit | U.S. Geological Survey, Earthquake Hazards Program, ANSS Comprehensive Earthquake Catalog |
| sha256 | `c492ba0b9b220a5bf2ffe3bf3872d5cf6c26d92f00610560e0114d8ab361e6b7` |
| Written by | pandas 2.3.3, pyarrow 24.0.0 |

| Column | dtype | Why it is here |
|---|---|---|
| `time`, `updated` | `datetime64[ns, UTC]` | timestamps with a timezone |
| `id` | `object` (str) | high-cardinality string: 12,535 unique |
| `place` | `object` (str) | high-cardinality free text: 6,550 unique |
| `latitude`, `longitude`, `depth` | `float64` | floats |
| `mag` | `float64` | float with a NaN |
| `nst` | `Int64` | nullable integer: 3,535 nulls |
| `magType`, `net` | `category` | low-cardinality categoricals (9 and 15 values; `magType` has a null) |
| `type`, `status` | `object` (str) | low-cardinality strings (6 and 2 values) |

USGS revises events after the fact (the `updated` column), so re-running the query returns different
bytes. That is why the slice is committed and its sha256 is checked by a test: a re-slice must update
this file and the size figures in `test_real_dataset.py` in the same change.

### How it was made

```bash
curl -sS -o usgs_2024-01.csv \
  'https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&starttime=2024-01-01&endtime=2024-02-01&orderby=time-asc'
uv run python - <<'EOF'
import pandas as pd

df = pd.read_csv("usgs_2024-01.csv", parse_dates=["time", "updated"])
df = df[["time", "updated", "id", "place", "latitude", "longitude", "depth", "mag", "magType", "nst", "net", "type", "status"]]
df = df.astype({"nst": "Int64", "magType": "category", "net": "category"})
df.to_parquet("usgs_earthquakes_2024-01.parquet", engine="pyarrow", compression="zstd", index=False)
EOF
sha256sum usgs_earthquakes_2024-01.parquet
```
