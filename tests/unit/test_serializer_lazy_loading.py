"""Unit tests for lazy loading of optional serializers.

Tests the lazy import mechanism for ArrowSerializer which requires
the optional [data] extra (pyarrow).
"""

from __future__ import annotations

import pytest

from cachekit.serializers import (
    SERIALIZER_REGISTRY,
    _get_arrow_serializer,
    benchmark_serializers,
    get_available_serializers,
    get_serializer,
    get_serializer_info,
)
from cachekit.serializers.arrow_serializer import ArrowSerializer
from cachekit.serializers.base import SerializerProtocol


class TestLazyArrowSerializerLoading:
    """Test lazy loading mechanism for ArrowSerializer."""

    def test_registry_has_none_for_arrow(self):
        """SERIALIZER_REGISTRY stores None for arrow (lazy placeholder)."""
        assert "arrow" in SERIALIZER_REGISTRY
        assert SERIALIZER_REGISTRY["arrow"] is None

    def test_get_arrow_serializer_returns_class(self):
        """_get_arrow_serializer() returns ArrowSerializer class."""
        cls = _get_arrow_serializer()
        assert cls is ArrowSerializer

    def test_get_arrow_serializer_caches_result(self):
        """_get_arrow_serializer() caches the imported class."""
        cls1 = _get_arrow_serializer()
        cls2 = _get_arrow_serializer()
        assert cls1 is cls2

    def test_get_serializer_arrow_returns_instance(self):
        """get_serializer('arrow') returns ArrowSerializer instance."""
        serializer = get_serializer("arrow")
        assert isinstance(serializer, ArrowSerializer)
        assert isinstance(serializer, SerializerProtocol)

    def test_get_serializer_arrow_with_integrity_checking(self):
        """get_serializer('arrow', enable_integrity_checking=False) works."""
        serializer = get_serializer("arrow", enable_integrity_checking=False)
        assert isinstance(serializer, ArrowSerializer)
        assert serializer.enable_integrity_checking is False

    def test_module_getattr_returns_arrow_serializer(self):
        """Module __getattr__ returns ArrowSerializer for lazy access."""
        from cachekit import serializers

        # Access via module attribute (triggers __getattr__)
        cls = serializers.ArrowSerializer
        assert cls is ArrowSerializer

    def test_module_getattr_raises_for_unknown(self):
        """Module __getattr__ raises AttributeError for unknown names."""
        from cachekit import serializers

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = serializers.NonExistentSerializer


class TestBenchmarkSerializersWithLazyLoading:
    """Test benchmark_serializers handles lazy loading."""

    def test_benchmark_serializers_includes_arrow(self):
        """benchmark_serializers() successfully instantiates arrow."""
        serializers = benchmark_serializers()
        assert "arrow" in serializers
        assert isinstance(serializers["arrow"], ArrowSerializer)

    def test_benchmark_serializers_returns_available_serializers(self):
        """benchmark_serializers() returns serializers that can be instantiated."""
        serializers = benchmark_serializers()
        # Should have core serializers (encrypted needs master key, so excluded)
        assert "auto" in serializers
        assert "default" in serializers
        assert "arrow" in serializers
        assert "orjson" in serializers
        # encrypted may be missing if no master key configured


class TestGetSerializerInfoWithLazyLoading:
    """Test get_serializer_info handles lazy loading."""

    def test_get_serializer_info_includes_arrow(self):
        """get_serializer_info() includes arrow with availability info."""
        info = get_serializer_info()
        assert "arrow" in info
        assert info["arrow"]["available"] is True
        assert info["arrow"]["class"] == "ArrowSerializer"

    def test_get_serializer_info_returns_all_serializers(self):
        """get_serializer_info() returns info for all registered serializers."""
        info = get_serializer_info()
        for name in SERIALIZER_REGISTRY:
            assert name in info
            assert "available" in info[name]
            assert "class" in info[name]

    def test_get_serializer_info_includes_get_info_data(self):
        """get_serializer_info() includes data from serializer.get_info() if available."""
        info = get_serializer_info()
        # ArrowSerializer has get_info method
        arrow_info = info["arrow"]
        assert arrow_info["available"] is True
        # get_info data should be merged in
        assert "module" in arrow_info


class TestGetAvailableSerializers:
    """Test get_available_serializers returns registry copy."""

    def test_returns_registry_copy(self):
        """get_available_serializers() returns a copy of the registry."""
        available = get_available_serializers()
        assert available == SERIALIZER_REGISTRY
        # Should be a copy, not the same object
        assert available is not SERIALIZER_REGISTRY

    def test_arrow_is_none_in_registry(self):
        """Arrow entry is None in the raw registry (lazy placeholder)."""
        available = get_available_serializers()
        assert available["arrow"] is None
