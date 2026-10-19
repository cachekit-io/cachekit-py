"""Compression in the cache serializer: AutoSerializer's ByteStorage envelope (LZ4 + xxHash3-64)."""

import random

import pytest

from cachekit.serializers.auto_serializer import AutoSerializer


class TestAutoSerializerCompression:
    """With integrity checking on (the default), general values are LZ4-compressed inside the envelope."""

    @pytest.mark.parametrize(
        "data",
        ["x" * 1000, {"data": ["item"] * 100}, b"binary" * 500, {"data": [{"id": i, "value": "test"} for i in range(100)]}],
    )
    def test_compression_roundtrip(self, data):
        serializer = AutoSerializer()
        serialized, metadata = serializer.serialize(data)
        assert metadata.compressed is True
        assert len(serialized) < len(repr(data))
        assert serializer.deserialize(serialized, metadata) == data

    def test_compression_efficiency(self):
        serializer = AutoSerializer()
        data = "a" * 10000
        serialized, metadata = serializer.serialize(data)
        assert len(serialized) < 1000
        assert serializer.deserialize(serialized, metadata) == data

    def test_integrity_off_disables_compression(self):
        # enable_integrity_checking=False is the knob that turns the compressing envelope off
        serializer = AutoSerializer(enable_integrity_checking=False)
        data = "x" * 100000
        serialized, metadata = serializer.serialize(data)
        assert metadata.compressed is False
        assert len(serialized) > len(data)
        assert serializer.deserialize(serialized, metadata) == data

    def test_incompressible_data(self):
        serializer = AutoSerializer()
        random_data = random.Random(0).randbytes(1000)
        serialized, metadata = serializer.serialize(random_data)
        assert serializer.deserialize(serialized, metadata) == random_data
