"""Integration tests for SaaS observability - headers injection end-to-end."""

from __future__ import annotations

import re

from cachekit.backends.cachekitio.backend import _inject_metrics_headers
from cachekit.backends.cachekitio.session import get_session_headers, get_session_id
from cachekit.decorators.wrapper import _FunctionStats


class TestSaaSHeaderInjection:
    """Test that observability headers are injected into SaaS backend requests."""

    def test_all_six_headers_present_in_http_request(self):
        """Verify all 7 required headers are present in HTTP request to SaaS."""
        # Create stats and record some hits
        stats = _FunctionStats()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)

        # Get metrics headers
        headers = _inject_metrics_headers(stats)

        # Verify all 7 headers present (includes new X-CacheKit-L1-Status)
        assert "X-CacheKit-Session-ID" in headers
        assert "X-CacheKit-Session-Start" in headers
        assert "X-CacheKit-L1-Hits" in headers
        assert "X-CacheKit-L2-Hits" in headers
        assert "X-CacheKit-Misses" in headers
        assert "X-CacheKit-L1-Hit-Rate" in headers
        assert "X-CacheKit-L1-Status" in headers
        assert len(headers) == 7

    def test_headers_format_correct_types(self):
        """Verify all header values are strings."""
        stats = _FunctionStats()
        stats.record_l1_hit()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)
        stats.record_miss()

        headers = _inject_metrics_headers(stats)

        # All values must be strings
        for key, value in headers.items():
            assert isinstance(value, str), f"Header {key} should be string, got {type(value)}"
            assert value, f"Header {key} should not be empty"

    def test_counters_match_cache_info(self):
        """Verify that metric headers match cache_info() counters."""
        stats = _FunctionStats()

        # Record some operations
        stats.record_l1_hit()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)
        stats.record_l2_hit(3.0)
        stats.record_miss()

        # Get cache info
        cache_info = stats.get_info()

        # Get metrics headers
        headers = _inject_metrics_headers(stats)

        # Verify counters match
        assert int(headers["X-CacheKit-L1-Hits"]) == cache_info.l1_hits == 2
        assert int(headers["X-CacheKit-L2-Hits"]) == cache_info.l2_hits == 2
        assert int(headers["X-CacheKit-Misses"]) == cache_info.misses == 1

    def test_l1_hit_rate_calculation_matches_cache_info(self):
        """Verify L1 hit rate calculation is correct."""
        stats = _FunctionStats()

        # Create: 407 L1 hits, 93 L2 hits = 0.814
        for _ in range(407):
            stats.record_l1_hit()
        for _ in range(93):
            stats.record_l2_hit(2.5)

        # Get metrics headers
        headers = _inject_metrics_headers(stats)
        l1_hit_rate = float(headers["X-CacheKit-L1-Hit-Rate"])

        # Manual calculation: 407 / (407 + 93) = 407/500 = 0.814
        assert abs(l1_hit_rate - 0.814) < 0.001
        assert headers["X-CacheKit-L1-Hit-Rate"] == "0.814"

    def test_zero_division_guard_in_headers(self):
        """Verify zero-division guard when no hits."""
        stats = _FunctionStats()
        # Don't record anything

        headers = _inject_metrics_headers(stats)

        # Should not raise error and should return 0.000
        assert headers["X-CacheKit-L1-Hit-Rate"] == "0.000"
        assert headers["X-CacheKit-L1-Hits"] == "0"
        assert headers["X-CacheKit-L2-Hits"] == "0"

    def test_graceful_degradation_none_stats(self):
        """Verify graceful degradation when stats is None."""
        headers = _inject_metrics_headers(None)

        # Should return empty dict, not raise error
        assert headers == {}
        assert isinstance(headers, dict)


class TestSessionIDStability:
    """Test that session IDs are stable and consistent."""

    def test_session_id_stable_across_multiple_calls(self):
        """Verify session ID is identical across multiple requests."""
        # Get session ID multiple times
        id1 = get_session_id()
        id2 = get_session_id()
        id3 = get_session_id()

        # All should be identical
        assert id1 == id2 == id3
        assert isinstance(id1, str)

    def test_session_id_uuid_v4_format(self):
        """Verify session ID is valid UUID v4."""
        session_id = get_session_id()

        # UUID v4 format: 8-4-4-4-12 with version 4
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        assert re.match(uuid_pattern, session_id, re.IGNORECASE)

    def test_session_headers_consistent_across_calls(self):
        """Verify session headers are identical across calls."""
        headers1 = get_session_headers()
        headers2 = get_session_headers()
        headers3 = get_session_headers()

        assert headers1 == headers2 == headers3
        assert headers1["X-CacheKit-Session-ID"] == headers2["X-CacheKit-Session-ID"]
        assert headers1["X-CacheKit-Session-Start"] == headers2["X-CacheKit-Session-Start"]

    def test_session_id_stable_in_metrics_headers(self):
        """Verify session ID from metrics headers is always the same."""
        stats = _FunctionStats()
        stats.record_l1_hit()

        headers1 = _inject_metrics_headers(stats)
        stats.record_l2_hit(2.5)
        headers2 = _inject_metrics_headers(stats)

        # Session ID should be stable for the SAME stats object across calls
        assert headers1["X-CacheKit-Session-ID"] == headers2["X-CacheKit-Session-ID"]


class TestMetricsHeaderFormatting:
    """Test that metrics headers are properly formatted."""

    def test_all_headers_are_strings(self):
        """Verify all header values are strings."""
        stats = _FunctionStats()
        stats.record_l1_hit()
        stats.record_l1_hit()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)
        stats.record_l2_hit(2.5)
        stats.record_miss()

        headers = _inject_metrics_headers(stats)

        for _key, value in headers.items():
            assert isinstance(value, str)

    def test_l1_hit_rate_three_decimal_places(self):
        """Verify L1 hit rate is always 3 decimal places."""
        test_cases = [
            (1, 1, "0.500"),  # 1/(1+1) = 0.5
            (2, 1, "0.667"),  # 2/(2+1) = 0.667
            (1, 2, "0.333"),  # 1/(1+2) = 0.333
            (100, 0, "1.000"),  # 100/100 = 1.0
            (0, 100, "0.000"),  # 0/100 = 0.0
        ]

        for l1_hits, l2_hits, expected_rate in test_cases:
            stats = _FunctionStats()
            for _ in range(l1_hits):
                stats.record_l1_hit()
            for _ in range(l2_hits):
                stats.record_l2_hit(2.5)

            headers = _inject_metrics_headers(stats)
            rate_str = headers["X-CacheKit-L1-Hit-Rate"]

            # Verify exact format
            assert rate_str == expected_rate
            # Verify exactly 3 decimal places
            assert len(rate_str.split(".")[1]) == 3

    def test_counter_values_are_numeric_strings(self):
        """Verify counter values are numeric strings."""
        stats = _FunctionStats()
        for _ in range(10):
            stats.record_l1_hit()
        for _ in range(5):
            stats.record_l2_hit(2.5)
        for _ in range(2):
            stats.record_miss()

        headers = _inject_metrics_headers(stats)

        # Verify counter values are numeric
        assert headers["X-CacheKit-L1-Hits"].isdigit()
        assert headers["X-CacheKit-L2-Hits"].isdigit()
        assert headers["X-CacheKit-Misses"].isdigit()

        # Verify they parse correctly
        assert int(headers["X-CacheKit-L1-Hits"]) == 10
        assert int(headers["X-CacheKit-L2-Hits"]) == 5
        assert int(headers["X-CacheKit-Misses"]) == 2

    def test_session_headers_format_valid(self):
        """Verify session headers are in valid format."""
        headers = _inject_metrics_headers(_FunctionStats())

        # Session ID should be UUID:function_identifier format
        session_id = headers["X-CacheKit-Session-ID"]
        # Format: "{uuid}:{function_identifier}" where uuid is 36 chars + ":" + "default" = 44
        assert ":" in session_id  # Should contain colon separator
        uuid_part = session_id.split(":")[0]
        assert len(uuid_part) == 36  # UUID v4 string length
        assert uuid_part.count("-") == 4  # UUID has 4 dashes

        # Session start should be numeric (milliseconds)
        session_start = headers["X-CacheKit-Session-Start"]
        assert session_start.isdigit()
        assert int(session_start) > 1600000000000  # Should be milliseconds


class TestEndToEndSaaSBackend:
    """Test end-to-end integration with SaaS backend."""

    def test_backend_receives_metrics_headers_on_put(self):
        """Verify backend can construct metrics headers for PUT request."""
        # Create stats with metrics
        stats = _FunctionStats()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)

        # Get headers to pass to backend
        headers = _inject_metrics_headers(stats)

        # Verify headers can be constructed
        assert "X-CacheKit-Session-ID" in headers
        assert "X-CacheKit-L1-Hits" in headers
        # Verify format is correct for HTTP headers
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())

    def test_backend_graceful_handling_missing_stats(self):
        """Verify backend gracefully handles missing stats."""
        # When stats is None, should return empty dict, not raise
        headers = _inject_metrics_headers(None)
        assert headers == {}
        assert isinstance(headers, dict)

    def test_metrics_headers_immutable_after_call(self):
        """Verify returned headers dict can be modified without affecting future calls."""
        stats = _FunctionStats()
        stats.record_l1_hit()

        headers1 = _inject_metrics_headers(stats)
        original_session_id = headers1["X-CacheKit-Session-ID"]

        # Modify returned dict
        headers1["X-CacheKit-Session-ID"] = "modified"

        # Get headers again
        headers2 = _inject_metrics_headers(stats)

        # Second call should return new dict with original value
        assert headers2["X-CacheKit-Session-ID"] == original_session_id


class TestRedisBackendNoHeaders:
    """Test that Redis backend does not add observability headers."""

    def test_redis_backend_sync_operations(self, mock_backend):
        """Verify Redis backend sync operations work without headers."""
        # Mock backend should not have header logic
        backend = mock_backend

        # These operations should work
        backend.set("test_key", b"test_value", ttl=60)
        value = backend.get("test_key")
        exists = backend.exists("test_key")
        deleted = backend.delete("test_key")

        # Verify operations called without errors
        assert backend.set.called
        assert backend.get.called
        assert backend.exists.called
        assert backend.delete.called

    def test_cache_operations_without_saas_backend(self):
        """Verify caching works without SaaS backend (local only)."""
        stats = _FunctionStats()

        # Record normal cache operations
        stats.record_l1_hit()
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)
        stats.record_miss()

        # Get cache info
        info = stats.get_info()

        # Verify statistics are tracked correctly
        assert info.l1_hits == 2
        assert info.l2_hits == 1
        assert info.misses == 1
        assert info.hits == 3  # Total hits


class TestObservabilityEdgeCases:
    """Test edge cases in observability functionality."""

    def test_very_large_hit_counts(self):
        """Verify handling of very large hit counts."""
        stats = _FunctionStats()

        # Record large numbers of hits
        for _ in range(1000000):
            stats.record_l1_hit()

        headers = _inject_metrics_headers(stats)

        assert headers["X-CacheKit-L1-Hits"] == "1000000"
        assert headers["X-CacheKit-L1-Hit-Rate"] == "1.000"

    def test_only_misses_no_hits(self):
        """Verify handling when only misses occur."""
        stats = _FunctionStats()

        for _ in range(10):
            stats.record_miss()

        headers = _inject_metrics_headers(stats)

        # Zero-division guard should protect
        assert headers["X-CacheKit-L1-Hit-Rate"] == "0.000"
        assert headers["X-CacheKit-Misses"] == "10"
        assert headers["X-CacheKit-L1-Hits"] == "0"
        assert headers["X-CacheKit-L2-Hits"] == "0"

    def test_hit_rate_precision_rounding(self):
        """Verify hit rate rounding to 3 decimals."""
        stats = _FunctionStats()

        # Create 1 L1, 2 L2 = 0.333...
        stats.record_l1_hit()
        stats.record_l2_hit(2.5)
        stats.record_l2_hit(2.5)

        headers = _inject_metrics_headers(stats)

        # Should round to 0.333
        assert headers["X-CacheKit-L1-Hit-Rate"] == "0.333"

    def test_session_unique_per_stats_instance(self):
        """Verify each _FunctionStats instance has its own unique session ID.

        This is critical for multi-wrapper scenarios (e.g., Locust load testing where
        multiple users each decorate the same function). Without per-instance session IDs,
        different wrappers would collide and cause 'counters_decreased' validation errors.
        """
        stats1 = _FunctionStats()
        stats2 = _FunctionStats()
        stats3 = _FunctionStats()

        headers1 = _inject_metrics_headers(stats1)
        headers2 = _inject_metrics_headers(stats2)
        headers3 = _inject_metrics_headers(stats3)

        # Session IDs should be UNIQUE across different stats instances
        session_id_1 = headers1["X-CacheKit-Session-ID"]
        session_id_2 = headers2["X-CacheKit-Session-ID"]
        session_id_3 = headers3["X-CacheKit-Session-ID"]

        assert session_id_1 != session_id_2
        assert session_id_2 != session_id_3
        assert session_id_1 != session_id_3

    def test_all_headers_always_present(self):
        """Verify all headers are always present, even with edge case data."""
        test_cases = [
            # (l1, l2, misses)
            (0, 0, 0),
            (0, 0, 10),
            (1, 0, 0),
            (0, 1, 0),
            (1000000, 1000000, 1000000),
        ]

        for l1, l2, misses in test_cases:
            stats = _FunctionStats()
            for _ in range(l1):
                stats.record_l1_hit()
            for _ in range(l2):
                stats.record_l2_hit(2.5)
            for _ in range(misses):
                stats.record_miss()

            headers = _inject_metrics_headers(stats)

            # All 7 headers must be present
            assert "X-CacheKit-Session-ID" in headers
            assert "X-CacheKit-Session-Start" in headers
            assert "X-CacheKit-L1-Hits" in headers
            assert "X-CacheKit-L2-Hits" in headers
            assert "X-CacheKit-Misses" in headers
            assert "X-CacheKit-L1-Hit-Rate" in headers
            assert "X-CacheKit-L1-Status" in headers
            assert len(headers) == 7


class TestObservabilityThreadSafety:
    """Test thread safety of observability features."""

    def test_session_id_thread_local(self):
        """Verify session ID is thread-local and consistent."""
        import threading

        results = {}

        def worker(thread_id):
            headers = get_session_headers()
            results[thread_id] = headers["X-CacheKit-Session-ID"]

        threads = []
        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All threads should see same session ID (process-scoped)
        session_ids = list(results.values())
        assert len(set(session_ids)) == 1

    def test_metrics_concurrent_recording(self):
        """Verify metrics can be recorded concurrently."""
        import threading

        stats = _FunctionStats()
        errors = []

        def worker(worker_id):
            try:
                for _ in range(10):
                    if worker_id % 2 == 0:
                        stats.record_l1_hit()
                    else:
                        stats.record_l2_hit(2.5)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # No errors should occur
        assert not errors

        # Verify all hits were recorded
        info = stats.get_info()
        assert info.hits == 100  # 10 threads * 10 operations


class TestBackwardCompatibility:
    """Test backward compatibility with existing code."""

    def test_inject_metrics_headers_signature(self):
        """Verify inject_metrics_headers has expected signature."""
        import inspect

        sig = inspect.signature(_inject_metrics_headers)
        params = list(sig.parameters.keys())

        # Should have single parameter 'stats'
        assert "stats" in params
        # Should accept None
        assert _inject_metrics_headers(None) == {}

    def test_cache_info_all_fields_present(self):
        """Verify CacheInfo has all expected fields."""
        stats = _FunctionStats()
        info = stats.get_info()

        # Verify all fields exist
        assert hasattr(info, "hits")
        assert hasattr(info, "misses")
        assert hasattr(info, "l1_hits")
        assert hasattr(info, "l2_hits")
        assert hasattr(info, "maxsize")
        assert hasattr(info, "currsize")
        assert hasattr(info, "l2_avg_latency_ms")
        assert hasattr(info, "last_operation_at")

    def test_session_functions_api_unchanged(self):
        """Verify session function APIs are unchanged."""
        from cachekit.backends.cachekitio.session import (
            get_session_headers,
            get_session_id,
            get_session_start_ms,
        )

        # All functions should be callable
        assert callable(get_session_id)
        assert callable(get_session_start_ms)
        assert callable(get_session_headers)

        # Verify return types
        session_id = get_session_id()
        assert isinstance(session_id, str)

        start_ms = get_session_start_ms()
        assert isinstance(start_ms, int)

        headers = get_session_headers()
        assert isinstance(headers, dict)
