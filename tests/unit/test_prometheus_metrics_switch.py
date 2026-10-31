"""``MonitoringConfig.enable_prometheus_metrics=False`` stops the cache-operation series.

``collect_stats`` and ``enable_prometheus_metrics`` each gate the three cache-operation series
(``cache_operations_total``, ``cache_operation_duration_ms``, ``cache_operation_size_bytes``);
``cache_info()`` counts calls whatever either says.
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit.config.nested import MonitoringConfig
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")


SERIES = ("cache_operations_total", "cache_operation_duration_ms_count", "cache_operation_size_bytes_count")


def _recorded(namespace: str) -> dict[str, float]:
    """Sum each cache-operation series over every label set of one namespace."""
    from prometheus_client import REGISTRY

    totals = dict.fromkeys(SERIES, 0.0)
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name in totals and sample.labels.get("namespace") == namespace:
                totals[sample.name] += sample.value
    return totals


# cache() or cache.dev: called with keyword overrides, it returns the decorator
DecoratorFactory = Callable[..., Callable[[Callable[[int], int]], Any]]


def _drive(decorate: DecoratorFactory, tmp_path: Path) -> tuple[dict[str, float], int]:
    """Make three calls (a miss, then two hits) and return the namespace's series totals and cache_info's count."""
    namespace = f"switch-{uuid.uuid4().hex}"
    backend = FileBackend(FileBackendConfig(cache_dir=tmp_path / namespace))

    @decorate(backend=backend, namespace=namespace, l1_enabled=False)
    def double(x: int) -> int:
        return x * 2

    # cache_info() stats are keyed by module and qualname, which every test here shares: count the delta
    before = double.cache_info()
    for _ in range(3):
        assert double(21) == 42
    after = double.cache_info()
    return _recorded(namespace), (after.hits + after.misses) - (before.hits + before.misses)


def test_enable_prometheus_metrics_false_records_no_cache_operations(tmp_path: Path) -> None:
    recorded, counted = _drive(
        lambda **kw: cache(monitoring=MonitoringConfig(collect_stats=True, enable_prometheus_metrics=False), **kw), tmp_path
    )
    assert recorded == dict.fromkeys(SERIES, 0.0)
    assert counted == 3


def test_default_monitoring_records_cache_operations(tmp_path: Path) -> None:
    recorded, counted = _drive(lambda **kw: cache(monitoring=MonitoringConfig(), **kw), tmp_path)
    assert recorded["cache_operations_total"] > 0
    assert counted == 3


def test_dev_preset_records_no_cache_operations(tmp_path: Path) -> None:
    recorded, counted = _drive(cache.dev, tmp_path)
    assert recorded == dict.fromkeys(SERIES, 0.0)
    assert counted == 3
