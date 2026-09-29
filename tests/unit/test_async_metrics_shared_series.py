"""Every AsyncMetricsCollector records into the documented series names.

Each decorated function builds its own collector, but prometheus_client's
default registry is process-wide. The second collector used to hit
"Duplicated timeseries" and register ``cache_operations_total_<uuid8>``,
which the documented PromQL never reads.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from typing import Any

import pytest

prometheus_client = pytest.importorskip("prometheus_client")

from cachekit import cache  # noqa: E402
from cachekit.reliability.async_metrics import AsyncMetricsCollector  # noqa: E402

REGISTRY = prometheus_client.REGISTRY
_UUID_SERIES = re.compile(r"^cache_operation(s_total|_duration_ms|_size_bytes)_[0-9a-f]{8}")


class _DictBackend:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


def _ops(namespace: str) -> float:
    """Sum cache_operations_total for a namespace, counting only the wrapper's hit/miss records."""
    return sum(
        sample.value
        for metric in REGISTRY.collect()
        for sample in metric.samples
        if sample.name == "cache_operations_total"
        and sample.labels.get("namespace") == namespace
        and sample.labels.get("serializer") in ("rust", "l1_memory")
    )


def _uuid_series() -> list[str]:
    return [sample.name for metric in REGISTRY.collect() for sample in metric.samples if _UUID_SERIES.match(sample.name)]


def test_two_collectors_share_documented_series() -> None:
    first, second = AsyncMetricsCollector(sync_mode=True), AsyncMetricsCollector(sync_mode=True)
    ns_a, ns_b = f"a-{uuid.uuid4().hex}", f"b-{uuid.uuid4().hex}"

    first.record_cache_operation("get", ns_a, True, 1.0, serializer="rust", size_bytes=10)
    second.record_cache_operation("get", ns_b, True, 1.0, serializer="rust", size_bytes=10)

    assert (_ops(ns_a), _ops(ns_b)) == (1.0, 1.0)
    assert _uuid_series() == []


def test_two_decorated_functions_both_increment_cache_operations_total() -> None:
    ns_a, ns_b = f"a-{uuid.uuid4().hex}", f"b-{uuid.uuid4().hex}"

    @cache(backend=_DictBackend(), namespace=ns_a, ttl=60)
    def fa(x: int) -> int:
        return x

    @cache(backend=_DictBackend(), namespace=ns_b, ttl=60)
    def fb(x: int) -> int:
        return x

    for fn in (fa, fb):
        fn(1)  # miss
        fn(1)  # hit

    assert _ops(ns_a) > 0
    assert _ops(ns_b) > 0
    assert _ops(ns_a) == _ops(ns_b)
    assert _uuid_series() == []


def test_name_owned_by_host_app_is_dropped_not_renamed(caplog: pytest.LogCaptureFixture) -> None:
    name = f"host_owned_{uuid.uuid4().hex}"
    prometheus_client.Counter(name, "registered by the host application", ["k"])

    collector = AsyncMetricsCollector(sync_mode=True)
    collector.record_counter(name, {"k": "v"})  # must not raise into the cache call

    assert "already registered outside cachekit" in caplog.text
    renamed = re.compile(rf"^{name}_[0-9a-f]{{8}}")
    assert not [s.name for m in REGISTRY.collect() for s in m.samples if renamed.match(s.name)]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_while_metric_lock_held_does_not_hang_child() -> None:
    import cachekit.reliability.async_metrics as am

    with am._metrics_cache_lock:  # stands in for another thread mid-registration at fork time
        pid = os.fork()
        if pid == 0:  # child: registering a fresh metric must not block on the inherited lock
            done = threading.Event()
            name = f"after_fork_{uuid.uuid4().hex}"
            threading.Thread(
                target=lambda: (AsyncMetricsCollector(sync_mode=True).record_counter(name, {"k": "v"}), done.set()), daemon=True
            ).start()
            os._exit(0 if done.wait(5) else 1)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
