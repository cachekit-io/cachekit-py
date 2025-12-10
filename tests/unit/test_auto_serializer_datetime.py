"""Unit tests for AutoSerializer datetime handling.

Tests comprehensive datetime, date, and time serialization support added
to fix 'can not serialize Timestamp object' errors.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from cachekit.serializers.auto_serializer import AutoSerializer


class TestAutoSerializerDatetime:
    """Test AutoSerializer with datetime, date, and time objects."""

    def test_serialize_datetime_object(self):
        """Test serializing a datetime object."""
        serializer = AutoSerializer()
        dt = datetime(2025, 11, 13, 15, 30, 45, 123456)
        data = {"timestamp": dt}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["timestamp"], datetime)
        assert deserialized["timestamp"] == dt

    def test_serialize_datetime_with_timezone(self):
        """Test serializing datetime with timezone info."""
        serializer = AutoSerializer()
        dt = datetime(2025, 11, 13, 15, 30, 45, tzinfo=timezone.utc)
        data = {"timestamp": dt}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["timestamp"], datetime)
        assert deserialized["timestamp"] == dt
        assert deserialized["timestamp"].tzinfo == timezone.utc

    def test_serialize_date_object(self):
        """Test serializing a date object."""
        serializer = AutoSerializer()
        d = date(2025, 11, 13)
        data = {"date": d}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["date"], date)
        assert deserialized["date"] == d

    def test_serialize_time_object(self):
        """Test serializing a time object."""
        serializer = AutoSerializer()
        t = time(15, 30, 45, 123456)
        data = {"time": t}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["time"], time)
        assert deserialized["time"] == t

    def test_serialize_list_with_datetimes(self):
        """Test serializing list containing datetime objects."""
        serializer = AutoSerializer()
        data = [
            datetime(2025, 1, 1),
            datetime(2025, 6, 15),
            datetime(2025, 12, 31),
        ]

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert len(deserialized) == 3
        assert all(isinstance(dt, datetime) for dt in deserialized)
        assert deserialized == data

    def test_serialize_complex_data_with_datetimes(self):
        """Test serializing complex nested structure with datetimes."""
        serializer = AutoSerializer()
        dt = datetime(2025, 11, 13, 15, 30, 45)
        data = {
            "user": {
                "id": 123,
                "name": "John",
                "created_at": dt,
                "tags": ["admin", "active"],
            },
            "events": [
                {"timestamp": datetime(2025, 11, 1, 10, 0, 0), "action": "login"},
                {"timestamp": datetime(2025, 11, 13, 15, 30, 45), "action": "update"},
            ],
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["user"]["created_at"], datetime)
        assert deserialized["user"]["created_at"] == dt
        assert isinstance(deserialized["events"][0]["timestamp"], datetime)
        assert deserialized["events"][1]["timestamp"] == datetime(2025, 11, 13, 15, 30, 45)

    def test_datetime_serialization_consistency(self):
        """Test that datetime serialization is consistent across multiple roundtrips."""
        serializer = AutoSerializer()
        dt = datetime(2025, 11, 13, 15, 30, 45, 123456)
        data = {"timestamp": dt}

        # First roundtrip
        serialized1, metadata1 = serializer.serialize(data)
        deserialized1 = serializer.deserialize(serialized1, metadata1)

        # Second roundtrip
        serialized2, metadata2 = serializer.serialize(deserialized1)
        deserialized2 = serializer.deserialize(serialized2, metadata2)

        # Should be identical
        assert deserialized1["timestamp"] == deserialized2["timestamp"]
        assert deserialized1["timestamp"] == dt

    def test_mixed_datetime_types(self):
        """Test serializing mixed datetime types in one structure."""
        serializer = AutoSerializer()
        data = {
            "datetime": datetime(2025, 11, 13, 15, 30, 45),
            "date": date(2025, 11, 13),
            "time": time(15, 30, 45),
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["datetime"], datetime)
        assert isinstance(deserialized["date"], date)
        assert isinstance(deserialized["time"], time)
        assert deserialized["datetime"] == data["datetime"]
        assert deserialized["date"] == data["date"]
        assert deserialized["time"] == data["time"]

    def test_datetime_in_dict_values(self):
        """Test datetime objects as dictionary values."""
        serializer = AutoSerializer()
        dt1 = datetime(2025, 1, 1, 0, 0, 0)
        dt2 = datetime(2025, 12, 31, 23, 59, 59)
        data = {"start": dt1, "end": dt2}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["start"] == dt1
        assert deserialized["end"] == dt2

    def test_datetime_with_microseconds(self):
        """Test datetime with microseconds precision."""
        serializer = AutoSerializer()
        dt = datetime(2025, 11, 13, 15, 30, 45, 999999)
        data = {"timestamp": dt}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["timestamp"] == dt
        assert deserialized["timestamp"].microsecond == 999999


class TestAutoSerializerDatetimeEdgeCases:
    """Edge case tests for datetime serialization."""

    def test_datetime_min_max_values(self):
        """Test serializing min/max datetime values."""
        serializer = AutoSerializer()
        data = {
            "min": datetime.min,
            "max": datetime.max,
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["min"] == datetime.min
        assert deserialized["max"] == datetime.max

    def test_date_min_max_values(self):
        """Test serializing min/max date values."""
        serializer = AutoSerializer()
        data = {
            "min": date.min,
            "max": date.max,
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["min"] == date.min
        assert deserialized["max"] == date.max

    def test_time_min_max_values(self):
        """Test serializing min/max time values."""
        serializer = AutoSerializer()
        data = {
            "min": time.min,
            "max": time.max,
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["min"] == time.min
        assert deserialized["max"] == time.max

    def test_empty_structure_with_datetime(self):
        """Test empty collections with datetime types."""
        serializer = AutoSerializer()
        data = {
            "empty_list": [],
            "datetime": datetime.now(),
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["empty_list"] == []
        assert isinstance(deserialized["datetime"], datetime)
