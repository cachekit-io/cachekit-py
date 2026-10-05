"""Unit tests for lazy loading of optional serializers.

Tests the lazy import mechanism for ArrowSerializer which requires
the optional [data] extra (pyarrow).
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

# Lazy loading can only be exercised when the lazily-loaded serializers are
# actually installed — requires the [data] + [json] extras, absent e.g. in the
# free-threaded CI lane (LAB-511).
pytest.importorskip("pyarrow")
pytest.importorskip("orjson")

from cachekit.serializers import (  # noqa: E402
    SERIALIZER_REGISTRY,
    _get_arrow_serializer,
    _get_orjson_serializer,
    benchmark_serializers,
    get_available_serializers,
    get_serializer,
    get_serializer_info,
)
from cachekit.serializers.arrow_serializer import ArrowSerializer  # noqa: E402
from cachekit.serializers.base import SerializerProtocol  # noqa: E402
from cachekit.serializers.orjson_serializer import OrjsonSerializer  # noqa: E402


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


class TestLazyOrjsonSerializerLoading:
    """Test lazy loading mechanism for OrjsonSerializer (optional [json] extra)."""

    def test_registry_has_none_for_orjson(self):
        """SERIALIZER_REGISTRY stores None for orjson (lazy placeholder)."""
        assert "orjson" in SERIALIZER_REGISTRY
        assert SERIALIZER_REGISTRY["orjson"] is None

    def test_get_orjson_serializer_returns_class(self):
        """_get_orjson_serializer() returns the OrjsonSerializer class."""
        assert _get_orjson_serializer() is OrjsonSerializer

    def test_get_orjson_serializer_caches_result(self):
        """_get_orjson_serializer() caches the imported class."""
        assert _get_orjson_serializer() is _get_orjson_serializer()

    def test_get_serializer_orjson_returns_instance(self):
        """get_serializer('orjson') returns an OrjsonSerializer instance."""
        serializer = get_serializer("orjson")
        assert isinstance(serializer, OrjsonSerializer)
        assert isinstance(serializer, SerializerProtocol)

    def test_module_getattr_returns_orjson_serializer(self):
        """Module __getattr__ returns OrjsonSerializer for lazy access."""
        from cachekit import serializers

        assert serializers.OrjsonSerializer is OrjsonSerializer

    def test_get_serializer_info_includes_orjson(self):
        """get_serializer_info() reports orjson as available with the right class."""
        info = get_serializer_info()
        assert info["orjson"]["available"] is True
        assert info["orjson"]["class"] == "OrjsonSerializer"

    def test_get_serializer_info_reports_orjson_unavailable(self, monkeypatch):
        """When orjson is absent, get_serializer_info() labels it OrjsonSerializer/unavailable.

        Guards the generalized optional-dep branch — before it was hardcoded to
        ArrowSerializer and would have mislabeled a missing orjson.
        """
        import cachekit.serializers as serializers_mod

        def _missing() -> type:
            raise ImportError("orjson is not installed. OrjsonSerializer requires the [json] extra")

        monkeypatch.setattr(serializers_mod, "_get_orjson_serializer", _missing)
        # Bypass the factory cache so get_serializer re-resolves orjson and the
        # ImportError reaches get_serializer_info's except branch.
        monkeypatch.delitem(serializers_mod._serializer_cache, "orjson:True", raising=False)

        info = serializers_mod.get_serializer_info()
        assert info["orjson"]["available"] is False
        assert info["orjson"]["class"] == "OrjsonSerializer"
        assert info["orjson"]["module"] == "cachekit.serializers.orjson_serializer"


class TestOrjsonIsOptional:
    """orjson is an optional dependency (the [json] extra): it must not be pulled
    eagerly, and when absent it must yield a helpful install error while the rest of
    cachekit keeps working. Verified in fresh subprocesses because sys.modules is
    shared across the test session (orjson is installed in the dev environment).
    """

    def test_import_cachekit_does_not_pull_orjson(self):
        """Importing cachekit must NOT eagerly import orjson (the optionality regression guard)."""
        code = "import cachekit, sys; assert 'orjson' not in sys.modules, 'orjson was imported eagerly'"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603 (trusted: sys.executable + literal code)
        assert result.returncode == 0, result.stderr

    def test_orjson_absent_raises_helpful_error(self):
        """Without orjson, cachekit + the default serializer still work, and requesting
        the orjson serializer raises a helpful, actionable [json]-extra ImportError."""
        code = (
            'import sys; sys.modules["orjson"] = None\n'
            "import cachekit\n"
            "from cachekit.serializers import get_serializer\n"
            'assert type(get_serializer("default")).__name__ == "StandardSerializer"\n'
            "try:\n"
            '    get_serializer("orjson")\n'
            '    raise SystemExit("expected ImportError")\n'
            "except ImportError as e:\n"
            '    assert "[json] extra" in str(e), str(e)\n'
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603 (trusted: sys.executable + literal code)
        assert result.returncode == 0, result.stderr


class TestDataStackLoadsOnFirstUse:
    """numpy, pandas and pyarrow (the [data] extra) load on first use, never at ``import cachekit``.

    They cost ~250 ms per process start, and pandas 2.x re-enables the GIL on free-threaded
    builds. Verified in fresh subprocesses because sys.modules is shared across the test session.
    """

    def test_import_cachekit_does_not_pull_the_data_stack(self):
        code = "import cachekit, sys; loaded = {'numpy', 'pandas', 'pyarrow'} & set(sys.modules); assert not loaded, loaded"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603 (trusted: sys.executable + literal code)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("integrity", [True, False])
    def test_first_decode_in_a_fresh_process_imports_on_demand(self, integrity):
        """Every data-stack entry decodes in a process that has not imported numpy or pandas."""
        import numpy as np
        import pandas as pd

        from cachekit.serializers import AutoSerializer

        values = {
            "ndarray": np.arange(6.0).reshape(2, 3),
            "nested": {"a": np.arange(3, dtype=np.int32)},
            "arrow": pd.DataFrame({"x": [1, 2], "y": ["a", None]}),
            "series": pd.Series([1.5, None], name="s"),
        }
        entries = {}
        for name, value in values.items():
            data, meta = AutoSerializer(enable_integrity_checking=integrity).serialize(value)
            entries[name] = (data.hex(), meta.to_dict())
        columnar = AutoSerializer(enable_integrity_checking=integrity)
        columnar._arrow_serializer = None  # force the msgpack-columnar DataFrame path
        data, meta = columnar.serialize(values["arrow"])
        entries["columnar"] = (data.hex(), meta.to_dict())

        code = (
            "import sys\n"
            "from cachekit.serializers import AutoSerializer\n"
            "from cachekit.serializers.base import SerializationMetadata\n"
            "assert not {'numpy', 'pandas', 'pyarrow'} & set(sys.modules)\n"
            f"for name, (data, meta) in {entries!r}.items():\n"
            f"    value = AutoSerializer(enable_integrity_checking={integrity}).deserialize(\n"
            "        bytes.fromhex(data), SerializationMetadata.from_dict(meta))\n"
            "    print(name, type(value).__name__)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)  # noqa: S603 (trusted: sys.executable + literal code)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [
            "ndarray",
            "ndarray",
            "nested",
            "dict",
            "arrow",
            "DataFrame",
            "series",
            "Series",
            "columnar",
            "DataFrame",
        ]

    def test_estimate_compression_ratio_reads_numpy_from_sys_modules(self):
        """estimate_compression_ratio() has no module-level numpy to lean on, plain values included."""
        import numpy as np

        from cachekit.serializers import AutoSerializer

        serializer = AutoSerializer()
        assert serializer.estimate_compression_ratio({"a": [1] * 100}) > 1
        assert serializer.estimate_compression_ratio(np.zeros(1000)) > 1

    @pytest.mark.parametrize(("pyarrow_installed", "level"), [(True, logging.WARNING), (False, logging.DEBUG)])
    def test_arrow_serializer_unavailable_is_logged(self, monkeypatch, caplog, pyarrow_installed, level):
        """No pyarrow falls back to msgpack columnar quietly; a pyarrow that is installed but fails to import warns."""
        from cachekit.serializers import AutoSerializer

        monkeypatch.setitem(sys.modules, "cachekit.serializers.arrow_serializer", None)
        if not pyarrow_installed:
            monkeypatch.setitem(sys.modules, "pyarrow", None)
        with caplog.at_level(logging.DEBUG, logger="cachekit.serializers.auto_serializer"):
            assert AutoSerializer()._arrow_serializer is None
        [record] = [r for r in caplog.records if "msgpack columnar fallback" in r.getMessage()]
        assert record.levelno == level

    @pytest.mark.parametrize("how", ["unloadable", "init_fails", "missing"])
    @pytest.mark.parametrize("path", ["numpy_raw", "nested_ndarray", "columnar_dataframe", "columnar_series"])
    def test_decode_without_the_data_stack_is_a_serialization_error(self, monkeypatch, path, how):
        """A missing or unloadable numpy/pandas is a SerializationError on every decode path, never ImportError/RuntimeError.

        A direct caller treats SerializationError as a miss and recomputes. "unloadable" is a package
        find_spec sees (the HAS_* flag is true) whose import fails, e.g. a broken native library;
        "init_fails" is one whose import raises RuntimeError, as an extension module's init can.
        """
        import numpy as np
        import pandas as pd

        from cachekit.serializers import AutoSerializer
        from cachekit.serializers import auto_serializer as auto
        from cachekit.serializers.base import SerializationError

        value, module = {
            "numpy_raw": (np.arange(3.0), "numpy"),
            "nested_ndarray": ({"a": np.arange(3)}, "numpy"),
            "columnar_dataframe": (pd.DataFrame({"x": [1.0, 2.0]}), "pandas"),
            "columnar_series": (pd.Series([1.5, 2.5], name="s"), "pandas"),
        }[path]
        serializer = AutoSerializer()
        serializer._arrow_serializer = None  # force the msgpack-columnar DataFrame path
        data, meta = serializer.serialize(value)

        if how == "unloadable":
            monkeypatch.setitem(sys.modules, module, None)
        elif how == "init_fails":
            real_import = auto.importlib.import_module

            def failing_import(name, package=None):
                if name == module:
                    raise RuntimeError("extension init failed")
                return real_import(name, package)

            monkeypatch.setattr(auto.importlib, "import_module", failing_import)
        else:
            monkeypatch.setattr(auto, f"HAS_{module.upper()}", False)
        with pytest.raises(SerializationError, match=r"cachekit\[data\]"):
            serializer.deserialize(data, meta)

    def test_arrow_classification_survives_a_pyarrow_module_without_a_spec(self, monkeypatch, caplog):
        """``find_spec`` raises ValueError for a sys.modules entry whose ``__spec__`` is None; that is a broken install, not a crash."""
        import types

        import pandas as pd

        from cachekit.serializers import AutoSerializer
        from cachekit.serializers.base import SerializationError

        df = pd.DataFrame({"x": [1.0, 2.0]})
        arrow_data, arrow_meta = AutoSerializer().serialize(df)
        assert arrow_meta.original_type == "arrow"

        monkeypatch.setitem(sys.modules, "pyarrow", types.ModuleType("pyarrow"))  # __spec__ is None
        monkeypatch.setitem(sys.modules, "cachekit.serializers.arrow_serializer", None)
        serializer = AutoSerializer()
        with caplog.at_level(logging.DEBUG, logger="cachekit.serializers.auto_serializer"):
            data, meta = serializer.serialize(df)
        assert meta.original_type == "dataframe"
        pd.testing.assert_frame_equal(serializer.deserialize(data, meta), df)
        with pytest.raises(SerializationError, match="ArrowSerializer not available"):
            serializer.deserialize(arrow_data, arrow_meta)
        [record] = [r for r in caplog.records if "msgpack columnar fallback" in r.getMessage()]
        assert record.levelno == logging.WARNING
