"""Cross-process invalidation events on Redis pub/sub.

After an invalidation's L2 change succeeded on a key-tracking backend, the decorator announces it
with one ``PUBLISH`` on :data:`CHANNEL`, on the backend's own client: no knob, no new connection,
no thread. The event is a MessagePack map of two strings, ``r`` (the function's registry id) and
``k`` (the invalidated cache key, absent when every key of the function was invalidated).

The channel is local to cachekit-py: no other SDK reads or writes it, and a format change takes a
new channel name, never a field negotiation.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import msgpack

from cachekit.hash_utils import redact_cache_key, redact_error_for_log

logger = logging.getLogger(__name__)

CHANNEL = "cachekit:py:invalidate:v1"

# Cap on each field, in UTF-8 bytes. A receiver rejects longer strings, so a longer key is
# announced as the whole function, which receivers already handle.
_MAX_FIELD_BYTES = 1024


def encode_event(registry_id: str, key: Optional[str]) -> Optional[bytes]:
    """The event announcing that ``key`` was invalidated, or every key of the function (``None``).

    Returns ``None`` when the registry id itself is over the cap: such an event could only be
    dropped by every receiver.

    Examples:
        >>> import msgpack
        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", "ns:ns:func:m.f:args:ab:1s"))
        {'r': 'ck:reg:ns:00ff', 'k': 'ns:ns:func:m.f:args:ab:1s'}
        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", None))
        {'r': 'ck:reg:ns:00ff'}

        A key over 1024 UTF-8 bytes widens the event to the whole function:

        >>> msgpack.unpackb(encode_event("ck:reg:ns:00ff", "鍵" * 400))
        {'r': 'ck:reg:ns:00ff'}
        >>> encode_event("ck:reg:" + "x" * 1024, None) is None
        True
    """
    if len(registry_id.encode("utf-8")) > _MAX_FIELD_BYTES:
        return None
    event = {"r": registry_id}
    if key is not None and len(key.encode("utf-8")) <= _MAX_FIELD_BYTES:
        event["k"] = key
    return msgpack.packb(event)


def publish(backend: Any, registry_id: str, key: Optional[str]) -> None:
    """Announce an invalidation whose L2 change already succeeded. Never raises.

    One ``PUBLISH`` on the backend's shared client, outside its error classification and the
    reliability stack, so a pub/sub failure cannot count against the circuit breaker. It waits for
    Redis's reply, never for delivery. A failure is a WARNING: the invalidation itself stands, and
    peers that missed the event keep their L1 copies until the L1 TTL.

    Args:
        backend: The resolved, key-tracking backend whose client carries the event
        registry_id: The function's unscoped registry id
        key: The invalidated cache key, or ``None`` for the whole function. Callers pass ``None``
            for custom ``key=`` functions, whose keys embed caller identifiers.
    """
    try:
        payload = encode_event(registry_id, key)
        if payload is None:
            logger.warning(
                "Invalidation not announced: registry id %s is over %d bytes (namespace too long)",
                redact_cache_key(registry_id),
                _MAX_FIELD_BYTES,
            )
            return
        receivers = backend._client.publish(CHANNEL, payload)
    except Exception as e:
        logger.warning(
            "Invalidation announcement failed; other processes keep their L1 copies until the L1 TTL: %s",
            redact_error_for_log(e),
        )
        return
    logger.debug("Invalidation announced to %s listener(s)", receivers)
