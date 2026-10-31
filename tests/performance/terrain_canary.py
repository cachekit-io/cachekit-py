"""Per-request latency of a ``@cache`` read workload, by the layer that served each call.

Drives a Zipfian read workload through one ``@cache`` function with L1 and a Redis L2, times every
call with ``perf_counter_ns``, and attributes it to the layer that answered by the change in
``cache_info()`` across the call: an L1 hit, an L2 hit, or a miss that ran the function. The
function also sets a flag when it runs, and a call whose flag and counters disagree is reported as
unattributed instead of being guessed at. The result is written as ``cachekit.terrain/v1``: for each
slice of time, one latency histogram per layer, 16 log-spaced bins per decade from 100 ns to 1 s.
The cachekit.io latency terrain replays one such recording.

What one figure covers:

  l1     key derivation, the L1 lookup and deserializing the stored bytes (with a backend
         configured, L1 holds serialized bytes, so an L1 hit costs more than an L1-only one;
         docs/performance.md)
  l2     the same lookup missing, one Redis round trip, deserializing, and the L1 backfill
  miss   the L2 lookup missing, the function itself, serializing and the L2 write

The workload: keys drawn from a Zipf distribution (exponent 0.99, the YCSB default) over
``--keys`` keys; each key's value is a seeded dict, about 1 KB as MessagePack (range roughly
0.2-6 KB). One caller makes the calls one at a time, at arrival times drawn as a Poisson process at
``--rate`` per second. A call that outlasts the gap to the next arrival makes the caller late; it
then starts the next call as soon as it is free and draws the following gap from there
(``next_arrival``), so it never runs above ``--rate`` to catch up. While misses dominate, the achieved rate is lower; the recording
states it, and the busiest slice's rate. A figure is the time one call took, not a response time
under load.

The function stands in for a database call with a fixed ``--origin-ms`` sleep, so a miss's figure is
that sleep plus the cache's own work. L1 gets a ``--l1-mb`` budget, smaller than the working set, so
the long tail falls through to L2. Values are stored unencrypted, as plain ``@cache`` stores them
(``@cache.secure`` encrypts).

Every run uses a fresh namespace, so it starts with L1 and L2 empty and the recording shows the
cache warming. Nothing is deleted: entries expire after ``--ttl`` seconds.

Run (from a checkout, after ``uv sync``, against a Redis you can write to)::

  uv run python tests/performance/terrain_canary.py --redis redis://127.0.0.1:6379/0 --minutes 10 \\
      --out terrain.json --harness https://github.com/cachekit-io/cachekit-py/blob/<sha>/tests/performance/terrain_canary.py

The summary printed at the end gives each layer's share and its exact p50 and p99 from the raw
samples; the histograms' own percentiles are only good to a bin (about 15%).
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import platform
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SCHEMA = "cachekit.terrain/v1"
LO = -7  # log10 seconds of the first bin's lower edge: 100 ns
PER_DECADE = 16
DECADES = 7  # up to 1 s
NBINS = DECADES * PER_DECADE
LAYERS = ("l1", "l2", "miss")
ZIPF_EXPONENT = 0.99
WORDS = (
    "cache layer tenant region shard replica index ledger order invoice customer product catalog price "
    "stock warehouse route session token profile avatar settings feed post comment thread reaction"
).split()


def bin_of(seconds: float) -> int:
    """Latency bin of a duration; durations outside 100 ns - 1 s clamp to the end bins.

    >>> bin_of(1e-6), bin_of(1e-3), bin_of(5.0), bin_of(0.0)
    (16, 64, 111, 0)
    """
    if seconds <= 0:
        return 0
    return min(NBINS - 1, max(0, math.floor((math.log10(seconds) - LO) * PER_DECADE)))


def payload(key: int) -> dict[str, Any]:
    """The value cached for a key: seeded, so a key always maps to the same value and size."""
    rng = random.Random(key)
    n = max(1, round(rng.lognormvariate(math.log(18), 0.8)))
    return {
        "id": key,
        "name": f"{rng.choice(WORDS)}-{key}",
        "updated": 1_760_000_000 + rng.randrange(10**7),
        "items": [
            {
                "sku": rng.randrange(10**8),
                "qty": rng.randrange(1, 9),
                "price": round(rng.uniform(1, 500), 2),
                "label": f"{rng.choice(WORDS)} {rng.choice(WORDS)}",
            }
            for _ in range(n)
        ],
    }


def next_arrival(due: float, now: float, gap: float) -> float:
    """When the next call starts: ``gap`` after the last call's start, or now if that has passed.

    ``due`` is when the last call was due to start and ``now`` is when the caller came free. A
    caller that keeps up starts every call on the Poisson schedule. One that is late starts the next
    call at once and draws the following gap from there: it drops the rest of the backlog instead of
    firing calls back to back to catch up, so every gap between starts is at least the drawn one and
    the call rate never runs above the arrival rate.

    >>> starts, due, free = [], 0.0, 0.0
    >>> for gap, service in [(1.0, 0.2), (1.0, 2.5), (1.0, 0.2), (1.0, 0.2)]:
    ...     due = next_arrival(due, free, gap)
    ...     starts.append(due)
    ...     free = due + service
    >>> starts  # on schedule, on schedule, late (starts when free), then one gap on
    [1.0, 2.0, 4.5, 5.5]
    """
    return max(due + gap, now)


class Slices:
    """Per-layer latency histograms for consecutive slices of wall-clock time."""

    def __init__(self, seconds: float, start: float) -> None:
        self.seconds = seconds
        self.start = start
        self.rows: list[dict[str, list[int]]] = []

    def add(self, now: float, layer: str, seconds: float) -> None:
        i = int((now - self.start) // self.seconds)
        while len(self.rows) <= i:
            self.rows.append({name: [0] * NBINS for name in LAYERS})
        self.rows[i][layer][bin_of(seconds)] += 1

    def pad(self, n: int) -> None:
        """Hold exactly ``n`` slices: a quiet stretch at the end still counts as time."""
        while len(self.rows) < n:
            self.rows.append({name: [0] * NBINS for name in LAYERS})
        del self.rows[n:]

    def to_json(self) -> list[dict[str, Any]]:
        out = []
        for i, row in enumerate(self.rows):
            out.append(
                {
                    "t": round(self.start + i * self.seconds, 3),
                    "n": [sum(row[name]) for name in LAYERS],
                    # Sparse: [bin, count, bin, count, ...] per layer.
                    "h": [[x for b, c in enumerate(row[name]) if c for x in (b, c)] for name in LAYERS],
                }
            )
        return out


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no samples (JSON has no NaN).

    >>> percentile([1.0, 2.0, 3.0, 4.0], 0.5), percentile([], 0.5)
    (2.0, None)
    """
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))]


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass  # no /proc (macOS, Windows) or unreadable: fall back to what platform reports
    return platform.processor() or platform.machine()


def run(args: argparse.Namespace) -> dict[str, Any]:
    import redis

    import cachekit
    from cachekit import cache
    from cachekit.backends.redis import RedisBackend
    from cachekit.config.nested import L1CacheConfig

    namespace = f"terrain-canary-{uuid.uuid4().hex[:12]}"
    executed = [False]

    @cache(
        backend=RedisBackend(redis_url=args.redis),
        ttl=args.ttl,
        namespace=namespace,
        encryption=False,
        l1=L1CacheConfig(max_size_mb=args.l1_mb, swr_enabled=False),
    )
    def fetch(key: int) -> dict[str, Any]:
        executed[0] = True
        time.sleep(args.origin_ms / 1000)
        return payload(key)

    rng = random.Random(args.seed)
    keys = range(args.keys)
    # Zipf: rank r drawn with probability proportional to 1 / (r + 1) ** ZIPF_EXPONENT.
    zipf_cdf = list(itertools.accumulate(1.0 / (r + 1) ** ZIPF_EXPONENT for r in keys))
    try:
        redis_version = redis.Redis.from_url(args.redis, socket_connect_timeout=5, socket_timeout=5).info("server")[
            "redis_version"
        ]
    except redis.RedisError as e:
        # The URL can carry a password: name the host, never the userinfo.
        u = urlsplit(args.redis)
        where = urlunsplit((u.scheme, u.netloc.rpartition("@")[2], u.path, "", ""))
        raise SystemExit(f"terrain_canary: cannot reach Redis at {where}: {e}") from e
    # Whole slices only: the run lasts a whole number of them, and a call still running when
    # the last one closes is not recorded, so no slice holds a partial window.
    n_slices = max(1, round(args.minutes * 60 / args.slice))
    duration = n_slices * args.slice
    start_wall = time.time()
    start = time.perf_counter()
    end = start + duration
    slices = Slices(args.slice, start_wall)
    samples: dict[str, list[float]] = {name: [] for name in LAYERS}
    unattributed = 0
    before = fetch.cache_info()
    due = start
    while True:
        due = next_arrival(due, time.perf_counter(), rng.expovariate(args.rate))
        if due >= end:
            break
        lag = due - time.perf_counter()
        if lag > 0:
            time.sleep(lag)
        key = rng.choices(keys, cum_weights=zipf_cdf)[0]
        executed[0] = False
        t0 = time.perf_counter_ns()
        fetch(key)
        t1 = time.perf_counter_ns()
        after = fetch.cache_info()
        moved = (after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits, after.misses - before.misses)
        before = after
        done = time.perf_counter()
        if done >= end:
            break
        layer = {(1, 0, 0): "l1", (0, 1, 0): "l2", (0, 0, 1): "miss"}.get(moved)
        if layer is None or (layer == "miss") != executed[0]:
            unattributed += 1
            continue
        seconds = (t1 - t0) / 1e9
        samples[layer].append(seconds)
        slices.add(start_wall + (done - start), layer, seconds)
    slices.pad(n_slices)

    attributed = sum(len(v) for v in samples.values())
    achieved = (attributed + unattributed) / duration
    busiest = max(sum(sum(row[name]) for name in LAYERS) for row in slices.rows) / args.slice
    summary = {}
    for name in LAYERS:
        values = sorted(samples[name])
        summary[name] = {
            "requests": len(values),
            "share": len(values) / attributed if attributed else 0.0,
            "p50_s": percentile(values, 0.5),
            "p99_s": percentile(values, 0.99),
        }
    return {
        "schema": SCHEMA,
        "source": {
            "kind": "recorded",
            "recordedAt": datetime.fromtimestamp(start_wall, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sdk": f"cachekit {cachekit.__version__} (Python {platform.python_version()})",
            "l2": f"Redis {redis_version}, {args.l2_where}",
            "cache": f"L1 budget {args.l1_mb} MB, ttl {args.ttl} s, unencrypted (plain @cache; @cache.secure encrypts)",
            # One caller cannot keep up while misses dominate, so the run states what it achieved,
            # overall and in its busiest slice, beside the arrival rate it was given.
            "workload": (
                f"Zipf {ZIPF_EXPONENT} over {args.keys:,} keys; one caller, one call at a time, Poisson arrivals "
                f"at {args.rate:g}/s (achieved {achieved:.0f}/s, busiest {args.slice:g} s slice {busiest:.0f}/s)"
            ),
            "origin": f"{args.origin_ms:g} ms sleep standing in for a database call",
            "host": f"{cpu_model()}, {platform.system()}",
            "harness": args.harness,
        },
        "bins": {"lo": LO, "perDecade": PER_DECADE, "n": NBINS},
        "sliceSeconds": args.slice,
        "layers": list(LAYERS),
        "slices": slices.to_json(),
        "summary": {
            "attributed": attributed,
            "unattributed": unattributed,
            "target_rate": args.rate,
            "achieved_rate": achieved,
            "busiest_slice_rate": busiest,
            "layers": summary,
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("--redis", required=True, help="Redis URL to use as L2 (the run writes under a fresh namespace)")
    p.add_argument("--l2-where", default="same host, loopback TCP", help="where the Redis runs, for the record")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--rate", type=float, default=200.0, help="calls per second (Poisson arrivals)")
    p.add_argument("--keys", type=int, default=10_000)
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--l1-mb", type=int, default=2)
    p.add_argument("--origin-ms", type=float, default=20.0)
    p.add_argument("--slice", type=float, default=3.0, help="seconds per histogram slice")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--harness", required=True, help="pinned URL of this file, recorded as the source")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    for name in ("minutes", "rate", "keys", "ttl", "l1_mb", "slice"):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} must be positive")
    if args.origin_ms < 0:
        p.error("--origin-ms must not be negative")
    if not args.out.parent.is_dir():
        p.error(f"--out: {args.out.parent} is not a directory")
    doc = run(args)
    text = json.dumps(doc, separators=(",", ":"), allow_nan=False)
    try:
        args.out.write_text(text)
    except OSError as e:
        # A run takes minutes: print the recording rather than lose it.
        print(text)
        print(f"terrain_canary: could not write {args.out} ({e}); the recording is on stdout above", file=sys.stderr)
        return 2
    s = doc["summary"]
    print(f"{s['attributed']:,} calls attributed, {s['unattributed']:,} unattributed -> {args.out}")
    for name, row in s["layers"].items():
        p50, p99 = (f"{v * 1e6:10.1f} us" if v is not None else "         -" for v in (row["p50_s"], row["p99_s"]))
        print(f"  {name:4}  {row['share']:7.2%}  p50 {p50}  p99 {p99}")
    return 0 if not s["unattributed"] else 1


if __name__ == "__main__":
    sys.exit(main())
