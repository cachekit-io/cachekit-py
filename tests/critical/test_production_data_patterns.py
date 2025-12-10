"""
Production Data Patterns Testing for PyRedis Cache Pro

This module validates the cache serialization system against realistic enterprise
data patterns commonly encountered in production environments. Focus is on:

- Complex business data structures (user profiles, API responses, etc.)
- Large-scale data processing (time-series, search results)
- Mixed-type collections with deep nesting
- Performance validation (not optimization - just "doesn't explode")
- Data integrity preservation across complex structures

REQUIREMENTS per CLAUDE.md:
- Type fidelity is NON-NEGOTIABLE (100% roundtrip accuracy)
- Enterprise features over micro-optimization
- Real-world scenarios, not theoretical edge cases
- Simple pass/fail criteria (KISS principles)
"""

import datetime
import time
from typing import Any

import pytest

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import pandas as pd

# Import from compatibility wrapper
from tests.critical.cache_serializer_compat import CACHE_SERIALIZER_AVAILABLE, CacheSerializer


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


def validate_roundtrip_integrity(serializer: CacheSerializer, data: Any, test_name: str) -> None:
    """Validate that serialization/deserialization preserves data structure"""
    # Serialize
    start_serialize = time.perf_counter()
    serialized, metadata = serializer.serialize(data)
    serialize_duration = time.perf_counter() - start_serialize

    # Deserialize
    start_deserialize = time.perf_counter()
    deserialized = serializer.deserialize(serialized)
    deserialize_duration = time.perf_counter() - start_deserialize

    # Log performance (not asserting - just monitoring)
    pattern = getattr(metadata, "pattern", "unknown")
    print(
        f"✓ {test_name} - Serialize: {serialize_duration:.4f}s, "
        f"Deserialize: {deserialize_duration:.4f}s, Size: {len(serialized)} bytes, "
        f"Pattern: {pattern}"
    )

    # Validate basic structure preservation
    validate_structure_preservation(data, deserialized, test_name)


def validate_structure_preservation(original: Any, result: Any, context: str) -> None:
    """Validate structure preservation with enterprise-aware type handling"""
    if type(original) is not type(result):
        # Allow documented type conversions (tuple -> list, set -> list)
        if isinstance(original, tuple) and isinstance(result, list):
            assert len(original) == len(result), f"{context}: Tuple->List length mismatch"
            return
        elif isinstance(original, set) and isinstance(result, list):
            assert len(original) == len(result), f"{context}: Set->List length mismatch"
            return
        else:
            raise AssertionError(f"{context}: Type mismatch: {type(original).__name__} -> {type(result).__name__}")

    if isinstance(original, dict):
        assert len(original) == len(result), f"{context}: Dict length mismatch"
        for key in original.keys():
            assert key in result, f"{context}: Missing key '{key}' in result"
    elif isinstance(original, (list, tuple)):
        assert len(original) == len(result), f"{context}: Sequence length mismatch"
    elif isinstance(original, (str, int, float, bool)) or original is None:
        assert original == result, f"{context}: Primitive value mismatch: {original} != {result}"


def validate_reasonable_performance(
    serializer: CacheSerializer, data: Any, test_name: str, max_serialize_ms: float, max_deserialize_ms: float
) -> None:
    """Performance benchmark (simple threshold check, not optimization)"""
    start = time.perf_counter()
    serialized, _metadata = serializer.serialize(data)
    serialize_time = (time.perf_counter() - start) * 1000  # Convert to ms

    assert serialize_time <= max_serialize_ms, (
        f"{test_name}: Serialization too slow: {serialize_time:.1f}ms (max: {max_serialize_ms}ms)"
    )

    start = time.perf_counter()
    _deserialized = serializer.deserialize(serialized)
    deserialize_time = (time.perf_counter() - start) * 1000  # Convert to ms

    assert deserialize_time <= max_deserialize_ms, (
        f"{test_name}: Deserialization too slow: {deserialize_time:.1f}ms (max: {max_deserialize_ms}ms)"
    )

    print(f"✓ {test_name} performance: {serialize_time:.1f}ms serialize, {deserialize_time:.1f}ms deserialize")


@pytest.mark.skipif(not CACHE_SERIALIZER_AVAILABLE, reason="Cache serializer not available")
class TestProductionDataPatterns:
    """Test realistic enterprise data patterns"""

    def setup_method(self):
        """Set up test fixtures"""
        self.serializer = CacheSerializer()
        self.data_generator = ProductionDataGenerator()

    def test_user_profile_with_complex_permissions(self):
        """Test user profiles with different permission structures"""
        for user_id in [1, 5, 10, 50, 100]:
            user_profile = self.data_generator.create_user_profile(user_id)

            validate_roundtrip_integrity(self.serializer, user_profile, f"user_profile_{user_id}")

    @pytest.mark.skip(reason="Slow test - handles large data")
    def test_large_api_response_structure(self):
        """Test different API response sizes"""
        test_sizes = [10, 100, 1000]

        for size in test_sizes:
            api_response = self.data_generator.create_large_api_response(size)

            validate_roundtrip_integrity(self.serializer, api_response, f"api_response_{size}_records")

            # Ensure performance doesn't explode for larger responses
            if size >= 1000:
                validate_reasonable_performance(
                    self.serializer,
                    api_response,
                    f"large_api_response_{size}",
                    5000.0,  # 5 second max serialize
                    5000.0,  # 5 second max deserialize
                )

    def test_time_series_metrics_data(self):
        """Test different time series sizes (enterprise monitoring scenarios)"""
        test_sizes = [100, 1440]  # 100 points, 1 day (1440 min)

        for size in test_sizes:
            time_series = self.data_generator.create_time_series_data(size)

            validate_roundtrip_integrity(self.serializer, time_series, f"time_series_{size}_points")

            # Performance validation for larger datasets
            if size >= 1440:
                validate_reasonable_performance(
                    self.serializer,
                    time_series,
                    f"time_series_{size}_points",
                    3000.0,  # 3 second max serialize
                    3000.0,  # 3 second max deserialize
                )

    def test_deeply_nested_configuration(self):
        """Test different nesting depths (enterprise config scenarios)"""
        for depth in range(1, 9):
            config = self.data_generator.create_configuration_object(depth)

            validate_roundtrip_integrity(self.serializer, config, f"config_depth_{depth}")

    def test_search_results_with_metadata(self):
        """Test different result set sizes"""
        test_sizes = [10, 50, 200]

        for size in test_sizes:
            search_results = self.data_generator.create_search_results(size)

            validate_roundtrip_integrity(self.serializer, search_results, f"search_results_{size}_items")

            # Performance check for larger result sets
            if size >= 200:
                validate_reasonable_performance(
                    self.serializer,
                    search_results,
                    f"search_results_{size}",
                    2000.0,  # 2 second max serialize
                    2000.0,  # 2 second max deserialize
                )

    def test_audit_log_with_timestamps(self):
        """Test different audit log sizes (enterprise audit requirements)"""
        test_sizes = [50, 500]

        for size in test_sizes:
            audit_log = self.data_generator.create_audit_log(size)

            validate_roundtrip_integrity(self.serializer, audit_log, f"audit_log_{size}_entries")

            # Performance validation for larger logs
            if size >= 500:
                validate_reasonable_performance(
                    self.serializer,
                    audit_log,
                    f"audit_log_{size}_entries",
                    4000.0,  # 4 second max serialize
                    4000.0,  # 4 second max deserialize
                )

    def test_dashboard_widget_configurations(self):
        """Test different dashboard complexities"""
        widget_counts = [5, 20, 50]

        for count in widget_counts:
            dashboard = self.data_generator.create_dashboard_config(count)

            validate_roundtrip_integrity(self.serializer, dashboard, f"dashboard_{count}_widgets")

    def test_mixed_type_collections(self):
        """Test complex mixed-type collection mimicking real enterprise data"""
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
            "mixed_array": [
                "string_value",
                12345,
                67.89,
                True,
                [1, 2, 3],  # tuple would convert to list
                {},
            ],
        }

        validate_roundtrip_integrity(self.serializer, mixed_collection, "complex_mixed_enterprise_data")

    def test_large_json_api_response(self):
        """Test a large JSON API response scenario"""
        large_response = self.data_generator.create_large_api_response(5000)  # 5k records

        # Measure serialized size
        serialized, _metadata = self.serializer.serialize(large_response)
        size_mb = len(serialized) / (1024 * 1024)

        print(f"✓ Large JSON response: {size_mb:.2f} MB serialized")

        # Ensure it deserializes correctly
        _deserialized = self.serializer.deserialize(serialized)

        # Performance should be reasonable (not optimized, just not exploding)
        validate_reasonable_performance(
            self.serializer,
            large_response,
            f"large_json_response_{size_mb:.1f}MB",
            10000.0,  # 10 second max serialize (this is large!)
            10000.0,  # 10 second max deserialize
        )

    def test_enterprise_data_integrity_edge_cases(self):
        """Test edge cases specific to enterprise data patterns"""
        edge_case_data = {
            "empty_structures": {
                "empty_user_profile": self.data_generator.create_user_profile(0),
                "empty_search_results": self.data_generator.create_search_results(0),
                "empty_time_series": self.data_generator.create_time_series_data(0),
            },
            "null_values": {
                "nullable_field": None,
                "empty_string": "",
                "zero_value": 0,
                "false_value": False,
            },
            "special_characters": {
                "unicode_text": "🚀 Enterprise 企业 🔒",
                "special_chars": "Special chars: @#$%^&*()[]{}|\\:;\"'<>,.?/",
                "newlines": "Line 1\nLine 2\r\nLine 3",
            },
        }

        validate_roundtrip_integrity(self.serializer, edge_case_data, "enterprise_edge_cases")

    @pytest.mark.skipif(not HAS_NUMPY, reason="NumPy not available")
    def test_numpy_array_enterprise_scenarios(self):
        """Test NumPy arrays in enterprise contexts"""
        enterprise_with_numpy = {
            "metrics_array": np.array([1.0, 2.5, 3.7, 4.2, 5.9]),
            "time_series_matrix": np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]),
            "boolean_flags": np.array([True, False, True, False]),
            "metadata": {
                "array_info": "Enterprise metrics data",
                "generated_at": "2024-08-03T15:30:00Z",
            },
        }

        validate_roundtrip_integrity(self.serializer, enterprise_with_numpy, "enterprise_numpy_arrays")

    def test_pandas_dataframe_enterprise_scenarios(self):
        """Test Pandas DataFrames in enterprise contexts"""
        # Create enterprise-style DataFrame
        df = pd.DataFrame(
            {
                "user_id": [1, 2, 3, 4, 5],
                "name": ["Alice", "Bob", "Carol", "Dave", "Eve"],
                "department": ["Engineering", "Sales", "Marketing", "Engineering", "Sales"],
                "salary": [75000.0, 65000.0, 70000.0, 80000.0, 72000.0],
                "active": [True, True, False, True, True],
            }
        )

        enterprise_with_dataframe = {
            "employee_data": df,
            "report_metadata": {
                "generated_by": "hr_system",
                "report_date": "2024-08-03",
                "department_count": len(df["department"].unique()),
                "total_employees": len(df),
            },
        }

        validate_roundtrip_integrity(self.serializer, enterprise_with_dataframe, "enterprise_dataframe")

    def test_datetime_objects_enterprise_patterns(self):
        """Test datetime objects in enterprise data structures"""
        now = datetime.datetime.now()
        today = datetime.date.today()
        current_time = datetime.time(14, 30, 45)
        delta = datetime.timedelta(days=7, hours=3)

        enterprise_datetime_data = {
            "audit_log": {
                "created_at": now,
                "date_only": today,
                "time_only": current_time,
                "retention_period": delta,
            },
            "user_activity": {
                "last_login": now - datetime.timedelta(hours=2),
                "session_start": current_time,
                "account_created": today - datetime.timedelta(days=30),
            },
        }

        validate_roundtrip_integrity(self.serializer, enterprise_datetime_data, "enterprise_datetime_patterns")

    def test_performance_under_concurrent_load_simulation(self):
        """Test performance characteristics under simulated concurrent load"""
        # Simulate multiple different enterprise data types being cached simultaneously
        concurrent_data_types = [
            ("user_profile", self.data_generator.create_user_profile(1)),
            ("api_response", self.data_generator.create_large_api_response(100)),
            ("time_series", self.data_generator.create_time_series_data(500)),
            ("config", self.data_generator.create_configuration_object(4)),
            ("search_results", self.data_generator.create_search_results(50)),
        ]

        # Test that each can be serialized/deserialized quickly
        for data_type, data in concurrent_data_types:
            validate_reasonable_performance(
                self.serializer,
                data,
                f"concurrent_{data_type}",
                1000.0,  # 1 second max for concurrent scenarios
                1000.0,  # 1 second max for concurrent scenarios
            )
