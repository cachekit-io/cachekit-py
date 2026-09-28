**[Home](../README.md)** › **[Serializers](README.md)** › **Custom Serializers**

# Custom Serializers

You can implement custom serializers by following the `SerializerProtocol` interface. This is the right approach when your data types aren't handled by the built-in serializers.

## SerializerProtocol Interface

```python
from cachekit.serializers.base import SerializerProtocol, SerializationMetadata
from typing import Any, Tuple
```

The protocol requires two methods:

- `serialize(obj: Any) -> Tuple[bytes, SerializationMetadata]`
- `deserialize(data: bytes, metadata: Any = None) -> Any`

`SerializerProtocol` is a `@runtime_checkable` protocol — you don't need to inherit from it, just implement the interface.

## Implementation Guide

```python
from cachekit.serializers.base import SerializerProtocol, SerializationMetadata
from typing import Any, Tuple

class CustomSerializer:
    """Custom serializer following SerializerProtocol."""

    def serialize(self, obj: Any) -> Tuple[bytes, SerializationMetadata]:
        """Serialize object to bytes with metadata."""
        # Your serialization logic here
        data = custom_encode(obj)
        metadata = SerializationMetadata(
            format="custom",
            compressed=False,
            encrypted=False,
            size_bytes=len(data)
        )
        return data, metadata

    def deserialize(self, data: bytes) -> Any:
        """Deserialize bytes back to object."""
        # Your deserialization logic here
        return custom_decode(data)
```

**Requirements:**
- Implement `serialize(obj) -> (bytes, SerializationMetadata)` method
- Implement `deserialize(bytes) -> Any` method
- Ensure round-trip fidelity: `deserialize(serialize(obj)[0]) == obj`

## Registration and Usage

Pass your serializer instance directly to the `@cache` decorator:

```python notest
# Use custom serializer
@cache(serializer=CustomSerializer())
def my_function():
    return special_data()
```

There is no global registration required — serializer instances are passed per decorator.

> [!IMPORTANT]
> **Your serializer gets its own cache-key code, derived from its class name.** The built-ins
> named as strings use a single character (`s`, `a`, `o`, `w`, `l`); every serializer passed
> as an *instance* — yours and the built-ins alike — encodes as `x` followed by four hex
> digits derived from its class name, which separates distinct classes in all but the rare
> case that two class names hash to the same four digits. Those four digits are a two-byte
> digest, so there are 65,536 codes and a collision is possible rather than impossible. It
> cannot mis-deserialize: the envelope records the serializer name and the mismatch guard
> rejects the entry. It does cost the isolation — two colliding classes share a key and miss
> on each other's entries. Give either one a distinct `namespace=` if you hit it.
>
> **Two instances of the same class do share one**, constructor arguments included. A
> `PydanticSerializer(model=User)` and a `PydanticSerializer(model=Order)` over the same
> function produce the same key *and* the same envelope name, so the serializer-mismatch
> guard cannot separate them either and one will deserialize the other's bytes. Give
> per-configuration serializers distinct `namespace=` values.

If you want to use a string alias (like `"custom"`) instead of passing an instance, you can register it in the serializer registry at import time:

```python notest
from cachekit.serializers import _registry  # internal, subject to change

_registry["custom"] = CustomSerializer
```

> [!NOTE]
> String alias registration uses an internal API that may change. Prefer passing instances directly for stability.

## Example: Pydantic Serializer

A practical example — a serializer that auto-converts Pydantic models:

```python
from pydantic import BaseModel
from cachekit.serializers.base import SerializerProtocol, SerializationMetadata
import msgpack
from typing import Any, Tuple

class PydanticSerializer:
    """Serializer that handles Pydantic models explicitly."""

    def serialize(self, obj: Any) -> Tuple[bytes, SerializationMetadata]:
        """Convert Pydantic models to dict before serializing."""
        if isinstance(obj, BaseModel):
            obj = obj.model_dump()

        data = msgpack.packb(obj)
        metadata = SerializationMetadata(
            format="MSGPACK",
            original_type="pydantic" if isinstance(obj, BaseModel) else "msgpack"
        )
        return data, metadata

    def deserialize(self, data: bytes, metadata: Any = None) -> Any:
        """Deserialize MessagePack bytes."""
        return msgpack.unpackb(data)

@cache(serializer=PydanticSerializer())
def get_user(user_id: int) -> dict:
    user = fetch_user_from_db(user_id)
    return user.model_dump()
```

---

## See Also

- [StandardSerializer](default.md) — General-purpose built-in serializer
- [OrjsonSerializer](orjson.md) — JSON-optimized built-in serializer
- [ArrowSerializer](arrow.md) — DataFrame-optimized built-in serializer
- [Caching Pydantic Models](pydantic.md) — Patterns for Pydantic model caching
- [API Reference](../api-reference.md) — SerializationMetadata fields and options

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
