**[Home](../README.md)** › **[Serializers](README.md)** › **Caching Pydantic Models**

# Caching Pydantic Models

**Issue:** Pydantic models are not directly serializable by StandardSerializer. This is intentional.

## Why Pydantic Models Aren't Auto-Detected

When you try to cache a Pydantic model directly:

```python
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str
    email: str

# WRONG - never cached: runs on every call
@cache
def get_user(user_id: int) -> User:
    return fetch_user_from_db(user_id)
```

Nothing raises. The default serializer cannot encode a Pydantic model, so each call returns the model, stores nothing, and logs an ERROR and a WARNING. See [Troubleshooting → Serialization Failures](../troubleshooting.md#common-errors). Only interop mode raises, with `InteropError`. L1-only (`backend=None`) differs: it stores the model object itself, so the model is cached, by reference, until you add a backend ([L1-Only Mode → Upgrade Path](../backends/none.md#upgrade-path)).

**Why we don't auto-detect Pydantic models:**

1. **Explicit is better than implicit** - Converting models to dicts without your knowledge is surprising
2. **Loss of fidelity** - `model.model_dump()` discards validators, computed fields, and methods
3. **Scope creep prevention** - Auto-detection for Pydantic opens the door to SQLAlchemy, dataclasses, ORMs, etc.

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
- Works with the default, `orjson` and `auto` serializers

## Alternative: Rebuild the Model After the Cache

If callers need a model instance with its methods, cache the dict and rebuild the model outside the cached function:

```python
import tempfile
from pathlib import Path

from pydantic import BaseModel
from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

class User(BaseModel):
    id: int
    name: str

    def is_admin(self) -> bool:
        return self.id < 10

@cache(backend=FileBackend(FileBackendConfig(cache_dir=Path(tempfile.mkdtemp()))), ttl=3600)
def get_user_data(user_id: int) -> dict:
    return User(id=user_id, name="Alice").model_dump()  # cache the data

def get_user(user_id: int) -> User:
    return User.model_validate(get_user_data(user_id))  # rebuild on every call

get_user(1)
user = get_user(1)  # served from the cache, rebuilt as a model
assert isinstance(user, User) and user.is_admin()
```

`model_validate` runs validation on every call, hit or miss. That is the price of getting a model back from a backend that stores bytes.

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

- [StandardSerializer](default.md) — The serializer used when caching dicts from `model_dump()`
- [Custom Serializers](custom.md) — Implement SerializerProtocol for specialized handling
- [API Reference](../api-reference.md) — Serializer parameters and options

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
