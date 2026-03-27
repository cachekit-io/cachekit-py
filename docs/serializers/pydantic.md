**[Home](../README.md)** › **[Serializers](./index.md)** › **Caching Pydantic Models**

# Caching Pydantic Models

**Issue:** Pydantic models are not directly serializable by DefaultSerializer. This is intentional.

## Why Pydantic Models Aren't Auto-Detected

When you try to cache a Pydantic model directly:

```python
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str
    email: str

# WRONG - Raises TypeError
@cache
def get_user(user_id: int) -> User:
    return fetch_user_from_db(user_id)  # Raises: User is not serializable
```

**Why we don't auto-detect Pydantic models:**

1. **Explicit is better than implicit** - Converting models to dicts without your knowledge is surprising
2. **Loss of fidelity** - `model.model_dump()` discards validators, computed fields, and methods
3. **Scope creep prevention** - Auto-detection for Pydantic opens the door to SQLAlchemy, dataclasses, ORMs, etc.
4. **Clear error messages** - The error tells you exactly what's wrong and how to fix it

## Recommended: Cache the Data, Not the Model

**Best practice** - Convert to dict before caching (explicit and efficient):

```python notest
# Illustrative example showing Pydantic model handling pattern
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str
    email: str

# RIGHT - Cache the data (dict), caller gets dict
@cache(ttl=3600)
def get_user(user_id: int) -> dict:
    user = fetch_user_from_db(user_id)  # Returns Pydantic model
    return user.model_dump()  # Convert to dict before caching

# Usage
data = get_user(123)  # Returns: {"id": 123, "name": "Alice", "email": "alice@example.com"}
print(data["name"])  # Works fine
```

**Advantages:**
- Explicit about what's being cached
- No validators/methods to lose
- Best performance (dict is optimal for MessagePack)
- Works with any serializer (Default, OrJSON, Arrow)

## Alternative: Use StandardSerializer for Full Model Instances

If you need the cached object to be a full Pydantic model instance with all methods:

```python notest
from pydantic import BaseModel
from cachekit import cache
from cachekit.serializers import StandardSerializer

class User(BaseModel):
    id: int
    name: str
    email: str

    def is_admin(self) -> bool:
        return self.id < 10  # Example computed property

# Cache the model data (note: methods not preserved, only data)
@cache(serializer=StandardSerializer(), ttl=3600, backend=None)
def get_user(user_id: int) -> User:
    return fetch_user_from_db(user_id)  # illustrative - not defined

# Usage
user = get_user(123)  # Returns: User(id=123, name="Alice", email="alice@example.com")
print(user.is_admin())  # Works - model reconstructed with methods
```

**Trade-offs:**
- ✅ Secure MessagePack serialization
- ✅ Portable across Python versions
- ✅ Pydantic models reconstructed correctly with all methods
- ❌ Larger serialized size

**When to use this approach:**
- You need model methods after deserialization
- You trust the cache source (internal Redis, not user-controlled)
- You're comfortable with Python-only serialization

## Advanced: Custom PydanticSerializer

If you have strong opinions about Pydantic handling, implement a custom serializer:

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

# Usage
@cache(serializer=PydanticSerializer())
def get_user(user_id: int) -> dict:
    user = fetch_user_from_db(user_id)
    return user.model_dump()
```

## Migration Path: Pydantic v1 → v2

**Pydantic v1 API:**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    return user.dict()  # Pydantic v1 method
```

**Pydantic v2 API (current):**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    return user.model_dump()  # Pydantic v2 method
```

**Future-proof approach (supports both):**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    # Works with Pydantic v1 or v2
    method = getattr(user, "model_dump", None) or getattr(user, "dict")
    return method()
```

---

## See Also

- [DefaultSerializer](./default.md) — The serializer used when caching dicts from `model_dump()`
- [Custom Serializers](./custom.md) — Implement SerializerProtocol for specialized handling
- [API Reference](../api-reference.md) — Serializer parameters and options
