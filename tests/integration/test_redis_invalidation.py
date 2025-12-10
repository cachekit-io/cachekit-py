"""Integration tests for RedisInvalidationChannel.

These tests require a running Redis instance and verify cross-pod invalidation
behavior with real Redis Pub/Sub messaging.
"""

import logging
import time
from dataclasses import dataclass

import pytest
import redis

from cachekit.invalidation.event import InvalidationEvent, InvalidationLevel
from cachekit.invalidation.redis_channel import RedisInvalidationChannel
from cachekit.l1_cache import L1Cache


@dataclass
class L1CacheConfig:
    """Configuration for L1Cache with invalidation support."""

    swr_enabled: bool = True
    swr_threshold_ratio: float = 0.5
    namespace_index: bool = True


@pytest.mark.integration
class TestRedisInvalidationChannel:
    """Integration tests for RedisInvalidationChannel with real Redis."""

    @pytest.fixture(autouse=True)
    def setup(self, skip_if_no_redis):
        """Set up test environment with Redis."""
        # Create Redis client
        self.redis_client = redis.Redis(host="localhost", port=6379, db=15, decode_responses=False)

        # Flush test database
        self.redis_client.flushdb()

        yield

        # Cleanup
        try:
            self.redis_client.flushdb()
            self.redis_client.close()
        except Exception:
            pass

    def test_publish_subscribe_roundtrip(self):
        """Published events should be received by subscribers."""
        received_events = []

        def callback(event):
            received_events.append(event)

        # Create channel and subscribe
        channel = RedisInvalidationChannel(self.redis_client)
        channel.subscribe(callback)
        channel.start()

        try:
            # Give listener thread time to start
            time.sleep(0.1)

            # Publish event
            event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            channel.publish(event)

            # Wait for event to propagate
            time.sleep(0.2)

            # Verify received
            assert len(received_events) == 1
            assert received_events[0].level == InvalidationLevel.GLOBAL
            assert received_events[0].namespace is None
            assert received_events[0].params_hash is None

        finally:
            channel.stop()

    def test_multiple_subscribers_receive_event(self):
        """Multiple subscribers should all receive the same event."""
        received_1 = []
        received_2 = []

        def callback_1(event):
            received_1.append(event)

        def callback_2(event):
            received_2.append(event)

        # Create channel with multiple subscribers
        channel = RedisInvalidationChannel(self.redis_client)
        channel.subscribe(callback_1)
        channel.subscribe(callback_2)
        channel.start()

        try:
            time.sleep(0.1)

            # Publish namespace invalidation
            event = InvalidationEvent(level=InvalidationLevel.NAMESPACE, namespace="user_cache", params_hash=None)
            channel.publish(event)

            time.sleep(0.2)

            # Both callbacks should receive event
            assert len(received_1) == 1
            assert len(received_2) == 1
            assert received_1[0].level == InvalidationLevel.NAMESPACE
            assert received_1[0].namespace == "user_cache"
            assert received_2[0].level == InvalidationLevel.NAMESPACE
            assert received_2[0].namespace == "user_cache"

        finally:
            channel.stop()

    def test_start_stop_lifecycle(self):
        """Channel should start and stop cleanly."""
        channel = RedisInvalidationChannel(self.redis_client)

        # Initial state
        assert not channel.is_available()

        # Start
        channel.start()
        assert channel.is_available()

        # Idempotent start (should not raise)
        channel.start()
        assert channel.is_available()

        # Stop
        channel.stop()
        assert not channel.is_available()

        # Idempotent stop (should not raise)
        channel.stop()
        assert not channel.is_available()

    def test_is_available_reflects_connection_state(self):
        """is_available() should accurately reflect connection state."""
        channel = RedisInvalidationChannel(self.redis_client)

        # Not started
        assert not channel.is_available()

        # After start
        channel.start()
        assert channel.is_available()

        # After stop
        channel.stop()
        assert not channel.is_available()

        # Can restart
        channel.start()
        assert channel.is_available()

        channel.stop()

    def test_invalidation_propagates_to_second_cache(self):
        """Cross-pod simulation: invalidation should clear entries in both caches."""
        # Create two L1 caches (simulating two pods)
        config = L1CacheConfig(namespace_index=True)
        cache1 = L1Cache(namespace="test", config=config)
        cache2 = L1Cache(namespace="test", config=config)

        # Create shared invalidation channel
        channel = RedisInvalidationChannel(self.redis_client)

        # Wire up invalidation callbacks
        def invalidate_cache1(event):
            if event.level == InvalidationLevel.GLOBAL:
                cache1.invalidate_all()
            elif event.level == InvalidationLevel.NAMESPACE and event.namespace:
                cache1.invalidate_by_namespace(event.namespace)
            elif event.level == InvalidationLevel.PARAMS and event.params_hash:
                cache1.invalidate_by_key(event.params_hash)

        def invalidate_cache2(event):
            if event.level == InvalidationLevel.GLOBAL:
                cache2.invalidate_all()
            elif event.level == InvalidationLevel.NAMESPACE and event.namespace:
                cache2.invalidate_by_namespace(event.namespace)
            elif event.level == InvalidationLevel.PARAMS and event.params_hash:
                cache2.invalidate_by_key(event.params_hash)

        channel.subscribe(invalidate_cache1)
        channel.subscribe(invalidate_cache2)
        channel.start()

        try:
            time.sleep(0.1)

            # Populate both caches with same data
            test_value = b"test_data"
            cache1.put("key1", test_value, redis_ttl=60.0, namespace="test_ns")
            cache2.put("key1", test_value, redis_ttl=60.0, namespace="test_ns")

            # Verify both have the entry
            hit1, _ = cache1.get("key1")
            hit2, _ = cache2.get("key1")
            assert hit1
            assert hit2

            # Publish global invalidation
            event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            channel.publish(event)

            # Wait for propagation
            time.sleep(0.2)

            # Both caches should be empty
            hit1, _ = cache1.get("key1")
            hit2, _ = cache2.get("key1")
            assert not hit1
            assert not hit2

        finally:
            channel.stop()

    def test_namespace_invalidation_clears_matching_entries(self):
        """Namespace invalidation should only clear entries in that namespace."""
        config = L1CacheConfig(namespace_index=True)
        cache1 = L1Cache(namespace="test", config=config)
        cache2 = L1Cache(namespace="test", config=config)

        channel = RedisInvalidationChannel(self.redis_client)

        def invalidate_cache1(event):
            if event.level == InvalidationLevel.NAMESPACE and event.namespace:
                cache1.invalidate_by_namespace(event.namespace)

        def invalidate_cache2(event):
            if event.level == InvalidationLevel.NAMESPACE and event.namespace:
                cache2.invalidate_by_namespace(event.namespace)

        channel.subscribe(invalidate_cache1)
        channel.subscribe(invalidate_cache2)
        channel.start()

        try:
            time.sleep(0.1)

            # Populate caches with different namespaces
            cache1.put("key1", b"data1", redis_ttl=60.0, namespace="users")
            cache1.put("key2", b"data2", redis_ttl=60.0, namespace="products")
            cache2.put("key1", b"data1", redis_ttl=60.0, namespace="users")
            cache2.put("key2", b"data2", redis_ttl=60.0, namespace="products")

            # Verify all entries exist
            assert cache1.get("key1")[0]
            assert cache1.get("key2")[0]
            assert cache2.get("key1")[0]
            assert cache2.get("key2")[0]

            # Invalidate only "users" namespace
            event = InvalidationEvent(level=InvalidationLevel.NAMESPACE, namespace="users", params_hash=None)
            channel.publish(event)

            time.sleep(0.2)

            # "users" entries should be gone, "products" should remain
            assert not cache1.get("key1")[0]  # users - cleared
            assert cache1.get("key2")[0]  # products - still there
            assert not cache2.get("key1")[0]  # users - cleared
            assert cache2.get("key2")[0]  # products - still there

        finally:
            channel.stop()

    def test_global_invalidation_clears_all_caches(self):
        """Global invalidation should clear all entries across all caches."""
        config = L1CacheConfig(namespace_index=True)
        cache1 = L1Cache(namespace="test", config=config)
        cache2 = L1Cache(namespace="test", config=config)

        channel = RedisInvalidationChannel(self.redis_client)

        def invalidate_cache1(event):
            if event.level == InvalidationLevel.GLOBAL:
                cache1.invalidate_all()

        def invalidate_cache2(event):
            if event.level == InvalidationLevel.GLOBAL:
                cache2.invalidate_all()

        channel.subscribe(invalidate_cache1)
        channel.subscribe(invalidate_cache2)
        channel.start()

        try:
            time.sleep(0.1)

            # Populate with multiple namespaces
            cache1.put("key1", b"data1", redis_ttl=60.0, namespace="ns1")
            cache1.put("key2", b"data2", redis_ttl=60.0, namespace="ns2")
            cache1.put("key3", b"data3", redis_ttl=60.0, namespace="ns3")
            cache2.put("key1", b"data1", redis_ttl=60.0, namespace="ns1")
            cache2.put("key2", b"data2", redis_ttl=60.0, namespace="ns2")

            # Publish global invalidation
            event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            channel.publish(event)

            time.sleep(0.2)

            # All entries should be cleared
            assert not cache1.get("key1")[0]
            assert not cache1.get("key2")[0]
            assert not cache1.get("key3")[0]
            assert not cache2.get("key1")[0]
            assert not cache2.get("key2")[0]

        finally:
            channel.stop()

    def test_publish_with_no_subscribers_logs_warning(self, caplog):
        """Publishing with no subscribers should log warning."""
        channel = RedisInvalidationChannel(self.redis_client)

        # Publish WITHOUT starting channel (no subscribers)
        with caplog.at_level(logging.WARNING):
            event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
            channel.publish(event)

        # Should log warning about no subscribers
        assert any("no subscribers" in record.message.lower() for record in caplog.records)

    def test_malformed_message_logged_and_skipped(self, caplog):
        """Malformed messages should be logged and processing should continue."""
        received_events = []

        def callback(event):
            received_events.append(event)

        channel = RedisInvalidationChannel(self.redis_client)
        channel.subscribe(callback)
        channel.start()

        try:
            time.sleep(0.1)

            with caplog.at_level(logging.WARNING):
                # Publish malformed message directly to Redis (bypass to_bytes())
                self.redis_client.publish("cachekit:invalidation", b"invalid msgpack data")

                # Give time to process
                time.sleep(0.2)

                # Should log warning about malformed message
                assert any("malformed" in record.message.lower() for record in caplog.records)

                # Now publish valid message
                event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)
                channel.publish(event)

                time.sleep(0.2)

                # Valid message should still be received
                assert len(received_events) == 1
                assert received_events[0].level == InvalidationLevel.GLOBAL

        finally:
            channel.stop()
