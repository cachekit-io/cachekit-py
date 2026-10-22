"""
Production data patterns through AutoSerializer (serializer="auto"), the type-preserving cache serializer.

Realistic business payloads (user profiles, API responses, time series, deep config, audit logs,
dashboards, mixed collections) must read back equal to what was written, with every container and
scalar type intact.
"""

import datetime
from typing import Any

import pytest

from cachekit.serializers.auto_serializer import AutoSerializer

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class ProductionDataGenerator:
    """Generate realistic enterprise data patterns for testing"""

    @staticmethod
    def create_user_profile(user_id: int) -> dict[str, Any]:
        """Generate user profile with complex permissions/roles structure"""
        return {
            "user_id": user_id,
            "email": f"user{user_id}@enterprise.com",
            "display_name": f"Enterprise User {user_id}",
            "active": True,
            "created_at": "2024-01-15T10:30:45Z",
            "last_login": "2024-08-03T14:22:33Z",
            # Complex permissions structure
            "permissions": {
                module: {
                    "read": True,
                    "write": user_id % 3 == 0,  # Only some users have write
                    "delete": user_id % 5 == 0,  # Fewer have delete
                    "admin": user_id % 10 == 0,  # Even fewer have admin
                }
                for module in ["users", "billing", "reports", "admin", "api", "analytics"]
            },
            # Roles array
            "roles": [
                "user",
                *(["manager"] if user_id % 5 == 0 else []),
                *(["admin"] if user_id % 10 == 0 else []),
                *(["super_admin"] if user_id % 20 == 0 else []),
            ],
            # Settings with mixed types
            "settings": {
                "timezone": "UTC",
                "language": "en",
                "theme": "dark",
                "notifications_enabled": True,
                "max_concurrent_sessions": 5,
                "session_timeout_minutes": 480,
                "two_factor_enabled": user_id % 3 == 0,
            },
            # Metadata
            "_metadata": {
                "version": 1,
                "data_source": "user_service_v2",
                "cached_at": "2024-08-03T15:30:00Z",
            },
        }

    @staticmethod
    def create_large_api_response(record_count: int) -> dict[str, Any]:
        """Generate large API response with multi-level nesting"""
        return {
            "status": "success",
            "timestamp": "2024-08-03T15:30:00Z",
            "total_records": record_count,
            "page": 1,
            "per_page": record_count,
            "has_more": False,
            # Generate records
            "data": [
                {
                    "id": i,
                    "name": f"Record {i}",
                    "status": "active" if i % 2 == 0 else "inactive",
                    "priority": (i % 5) + 1,
                    "score": i * 1.5,
                    # Nested attributes
                    "attributes": {
                        "category": f"cat_{i % 10}",
                        "subcategory": f"subcat_{i % 50}",
                        "tags": [f"tag_{i % 3}", f"type_{i % 7}", f"region_{i % 4}"],
                    },
                    # Nested metadata
                    "metadata": {
                        "created_by": f"user_{i % 100}",
                        "updated_by": f"user_{(i + 1) % 100}",
                        "version": (i % 3) + 1,
                    },
                }
                for i in range(record_count)
            ],
            # Aggregations
            "aggregations": {
                "total_active": record_count // 2,
                "total_inactive": record_count // 2,
                "avg_score": record_count * 0.75,
                "categories": {},  # Simplified for test
            },
        }

    @staticmethod
    def create_time_series_data(point_count: int) -> dict[str, Any]:
        """Generate time-series metrics data"""
        import math

        return {
            "metric_name": "cpu_utilization",
            "instance_id": "i-1234567890abcdef0",
            "start_time": "2024-08-03T00:00:00Z",
            "end_time": "2024-08-03T23:59:59Z",
            "resolution_seconds": 60,
            # Generate data points
            "data_points": [
                {
                    "timestamp": f"2024-08-03T{i // 60:02d}:{i % 60:02d}:00Z",
                    "value": 50.0 + math.sin(i * 0.1) * 30.0,  # Simulated CPU usage
                    "quality": "interpolated" if i % 100 == 0 else "measured",
                }
                for i in range(point_count)
            ],
            # Statistics
            "statistics": {
                "min": 20.0,
                "max": 80.0,
                "avg": 50.0,
                "p95": 75.0,
                "p99": 78.0,
                "count": point_count,
            },
        }

    @staticmethod
    def create_configuration_object(depth: int) -> dict[str, Any]:
        """Generate configuration object with mixed types and deep nesting"""

        def create_nested_section(current_depth: int) -> dict[str, Any]:
            section = {
                "level": current_depth,
                "setting_a": f"value_at_level_{current_depth}",
                "setting_b": current_depth * 10,
                "enabled": current_depth % 2 == 0,
            }

            if current_depth > 1:
                section["subsection"] = create_nested_section(current_depth - 1)

            return section

        config = {
            "app_name": "PyRedis Cache Pro",
            "version": "1.0.0",
            "environment": "production",
            "debug": False,
            # Database configuration
            "database": {
                "host": "redis.example.com",
                "port": 6379,
                "ssl": True,
                "max_connections": 100,
                "timeout_seconds": 30.0,
                # Connection pool settings
                "pool": {
                    "min_idle": 10,
                    "max_idle": 50,
                    "idle_timeout": 300,
                    "test_on_borrow": True,
                },
            },
            # Features configuration with nested toggles
            "features": {
                "circuit_breaker": True,
                "adaptive_timeout": True,
                "metrics_collection": True,
                "circuit_breaker_config": {
                    "failure_threshold": 5,
                    "timeout_seconds": 60.0,
                    "half_open_requests": 3,
                },
            },
        }

        # Deep nesting if requested
        if depth > 1:
            config["nested_config"] = create_nested_section(depth - 1)

        return config

    @staticmethod
    def create_search_results(result_count: int) -> dict[str, Any]:
        """Generate search results with metadata"""
        return {
            "query": "enterprise cache optimization",
            "total_hits": result_count * 10,  # More results exist
            "returned_hits": result_count,
            "search_time_ms": 45.67,
            "index_name": "enterprise_docs",
            "results": [
                {
                    "id": f"doc_{i}",
                    "title": f"Enterprise Document {i}",
                    "url": f"https://docs.enterprise.com/doc_{i}",
                    "score": 1.0 - (i * 0.01),
                    "excerpt": f"This document covers enterprise topic {i} in detail...",
                    "metadata": {
                        "document_type": "guide" if i % 3 == 0 else "reference",
                        "last_updated": "2024-07-15T10:00:00Z",
                        "word_count": 500 + (i * 50),
                        "reading_time_minutes": 3 + (i // 5),
                    },
                    "tags": [
                        f"category_{i % 5}",
                        "beginner" if i % 3 == 0 else "advanced",
                        "enterprise",
                        "production",
                    ],
                }
                for i in range(result_count)
            ],
            "facets": {
                "document_types": {
                    "guide": result_count // 3,
                    "reference": (result_count * 2) // 3,
                }
            },
        }

    @staticmethod
    def create_audit_log(entry_count: int) -> dict[str, Any]:
        """Generate audit log with timestamps and structured data"""
        actions = ["login", "logout", "create", "update", "delete", "view", "export"]
        resources = ["user", "document", "configuration", "audit_log", "cache"]

        return {
            "log_id": "audit_2024_08_03_001",
            "generated_at": "2024-08-03T15:30:00Z",
            "requested_by": "system_audit",
            "entries": [
                {
                    "timestamp": f"2024-08-03T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}Z",
                    "user_id": f"user_{i % 50}",
                    "action": actions[i % len(actions)],
                    "resource_type": resources[i % len(resources)],
                    "resource_id": f"{resources[i % len(resources)]}_{i}",
                    "success": i % 20 != 0,  # Occasional failures
                    "request_details": {
                        "ip_address": f"192.168.1.{i % 255}",
                        "user_agent": "Enterprise-App/1.0",
                        "session_id": f"sess_{i % 100}",
                    },
                    **(
                        {
                            "changes": {
                                "field_changed": "status",
                                "old_value": "inactive",
                                "new_value": "active",
                            }
                        }
                        if actions[i % len(actions)] == "update"
                        else {}
                    ),
                }
                for i in range(entry_count)
            ],
            "summary": {
                "total_entries": entry_count,
                "successful_operations": (entry_count * 19) // 20,
                "failed_operations": entry_count // 20,
            },
        }

    @staticmethod
    def create_dashboard_config(widget_count: int) -> dict[str, Any]:
        """Generate dashboard widget configuration with complex layout data"""
        widget_types = ["chart", "table", "metric", "text", "image"]

        return {
            "dashboard_id": "enterprise_overview",
            "name": "Enterprise Overview Dashboard",
            "description": "Main dashboard for enterprise metrics",
            "created_by": "admin_user",
            "created_at": "2024-01-15T10:00:00Z",
            "last_modified": "2024-08-03T14:30:00Z",
            "layout": {
                "columns": 12,
                "row_height": 60,
                "margin": [10, 10],
                "container_padding": [5, 5],
            },
            "widgets": [
                {
                    "id": f"widget_{i}",
                    "type": widget_types[i % len(widget_types)],
                    "title": f"Widget {i}",
                    "position": {
                        "x": (i % 4) * 3,
                        "y": (i // 4) * 2,
                        "width": 3,
                        "height": 2,
                    },
                    "configuration": {
                        **(
                            {
                                "chart_type": "line",
                                "data_source": "metrics_api",
                                "refresh_interval": 30,
                                "axes": {"x_axis": "timestamp", "y_axis": "value"},
                            }
                            if widget_types[i % len(widget_types)] == "chart"
                            else {}
                        )
                    },
                    "style": {
                        "background_color": "#ffffff",
                        "border_color": "#e0e0e0",
                        "text_color": "#333333",
                    },
                }
                for i in range(widget_count)
            ],
            "settings": {
                "auto_refresh": True,
                "refresh_interval_seconds": 300,
                "enable_notifications": True,
                "theme": "light",
            },
        }


def assert_identical(original: Any, result: Any, path: str = "value") -> None:
    """Deep equality that also requires every node to keep its exact type: == alone treats True, 1 and 1.0 as equal
    and cannot compare arrays."""
    assert type(result) is type(original), f"{path}: {type(original).__name__} read back as {type(result).__name__}"
    if isinstance(original, dict):
        assert result.keys() == original.keys(), f"{path}: keys differ"
        for key, value in original.items():
            assert_identical(value, result[key], f"{path}[{key!r}]")
    elif isinstance(original, (list, tuple)):
        assert len(result) == len(original), f"{path}: length differs"
        for i, (want, got) in enumerate(zip(original, result, strict=True)):
            assert_identical(want, got, f"{path}[{i}]")
    elif HAS_NUMPY and isinstance(original, np.ndarray):
        assert result.dtype == original.dtype, f"{path}: dtype differs"
        np.testing.assert_array_equal(result, original, err_msg=path)
    else:
        assert result == original, f"{path}: {original!r} read back as {result!r}"


def validate_roundtrip_integrity(serializer: AutoSerializer, data: Any) -> None:
    serialized, metadata = serializer.serialize(data)
    assert_identical(data, serializer.deserialize(serialized, metadata))


class TestProductionDataPatterns:
    """Realistic enterprise payloads must survive a round trip unchanged."""

    def setup_method(self):
        self.serializer = AutoSerializer()
        self.data_generator = ProductionDataGenerator()

    @pytest.mark.parametrize("user_id", [0, 1, 5, 10, 50, 100])
    def test_user_profile_with_complex_permissions(self, user_id):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_user_profile(user_id))

    @pytest.mark.parametrize("size", [0, 10, 100, 1000, 5000])
    def test_large_api_response_structure(self, size):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_large_api_response(size))

    @pytest.mark.parametrize("size", [0, 100, 1440])
    def test_time_series_metrics_data(self, size):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_time_series_data(size))

    @pytest.mark.parametrize("depth", range(1, 9))
    def test_deeply_nested_configuration(self, depth):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_configuration_object(depth))

    @pytest.mark.parametrize("size", [0, 10, 50, 200])
    def test_search_results_with_metadata(self, size):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_search_results(size))

    @pytest.mark.parametrize("size", [50, 500])
    def test_audit_log_with_timestamps(self, size):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_audit_log(size))

    @pytest.mark.parametrize("count", [5, 20, 50])
    def test_dashboard_widget_configurations(self, count):
        validate_roundtrip_integrity(self.serializer, self.data_generator.create_dashboard_config(count))

    def test_mixed_type_collections(self):
        mixed_collection = {
            "user_profile": self.data_generator.create_user_profile(42),
            "recent_searches": self.data_generator.create_search_results(10),
            "system_metrics": self.data_generator.create_time_series_data(100),
            "app_config": self.data_generator.create_configuration_object(3),
            "_metadata": {
                "cache_version": "2.1.0",
                "generated_at": "2024-08-03T15:30:00Z",
                "expires_at": "2024-08-03T16:30:00Z",
                "data_sources": ["user_service", "search_service", "metrics_service", "config_service"],
            },
            "mixed_array": ["string_value", 12345, 67.89, True, (1, 2, 3), {4, 5}, frozenset({"x"}), {}],
        }
        validate_roundtrip_integrity(self.serializer, mixed_collection)

    def test_enterprise_data_integrity_edge_cases(self):
        edge_case_data = {
            "null_values": {
                "nullable_field": None,
                "empty_string": "",
                "zero_value": 0,
                "zero_float": 0.0,
                "false_value": False,
            },
            "special_characters": {
                "unicode_text": "🚀 Enterprise 企业 🔒",
                "special_chars": "Special chars: @#$%^&*()[]{}|\\:;\"'<>,.?/",
                "newlines": "Line 1\nLine 2\r\nLine 3",
                "nul": "a\x00b",
            },
        }
        validate_roundtrip_integrity(self.serializer, edge_case_data)

    @pytest.mark.skipif(not HAS_NUMPY, reason="NumPy not available")
    def test_numpy_array_enterprise_scenarios(self):
        enterprise_with_numpy = {
            "metrics_array": np.array([1.0, 2.5, 3.7, 4.2, 5.9]),
            "time_series_matrix": np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]),
            "boolean_flags": np.array([True, False, True, False]),
            "metadata": {
                "array_info": "Enterprise metrics data",
                "generated_at": "2024-08-03T15:30:00Z",
            },
        }
        validate_roundtrip_integrity(self.serializer, enterprise_with_numpy)

    @pytest.mark.parametrize("kind", ["DataFrame", "Series"])
    def test_pandas_enterprise_scenarios_top_level_only(self, kind):
        # A DataFrame or Series is encoded only as the whole cached value. Nested in a dict it is refused,
        # and the error names that limit, as docs/serializers/auto.md does.
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame(
            {
                "user_id": [1, 2, 3, 4, 5],
                "name": ["Alice", "Bob", "Carol", "Dave", "Eve"],
                "department": ["Engineering", "Sales", "Marketing", "Engineering", "Sales"],
                "salary": [75000.0, 65000.0, 70000.0, 80000.0, 72000.0],
                "active": [True, True, False, True, True],
            }
        )
        value = df if kind == "DataFrame" else df["salary"]

        serialized, metadata = self.serializer.serialize(value)
        result = self.serializer.deserialize(serialized, metadata)
        if kind == "DataFrame":
            pd.testing.assert_frame_equal(result, value)
        else:
            pd.testing.assert_series_equal(result, value)

        enterprise_with_pandas = {
            "employee_data": value,
            "report_metadata": {"generated_by": "hr_system", "report_date": "2024-08-03", "total_employees": len(df)},
        }
        with pytest.raises(TypeError, match="pandas DataFrames and Series as the top-level value only"):
            self.serializer.serialize(enterprise_with_pandas)

    def test_datetime_objects_enterprise_patterns(self):
        # timedelta is not in AutoSerializer's supported set (datetime, date, time), so it is not part of this payload
        now = datetime.datetime.now()
        today = datetime.date.today()
        current_time = datetime.time(14, 30, 45)
        enterprise_datetime_data = {
            "audit_log": {
                "created_at": now,
                "created_at_utc": now.replace(tzinfo=datetime.timezone.utc),
                "date_only": today,
                "time_only": current_time,
            },
            "user_activity": {
                "last_login": now - datetime.timedelta(hours=2),
                "session_start": current_time,
                "account_created": today - datetime.timedelta(days=30),
            },
        }
        validate_roundtrip_integrity(self.serializer, enterprise_datetime_data)
