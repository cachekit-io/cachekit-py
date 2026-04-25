**[Home](../README.md)** › **Features** › **Reference Caching**

# Reference Caching with @cache.local()

**Available since v0.6.0**

## TL;DR

Reference caching (`@cache.local()`) caches opaque, non-serializable objects — things that can't be converted to bytes. Perfect for SDK connections, ML models, database connections, and language runtime objects that must maintain identity.

```python notest
from cachekit import cache

@cache.local()
def get_langfuse_client():
    """Returns same instance for identical args."""
    return Langfuse(project="my-project")

client1 = get_langfuse_client()
client2 = get_langfuse_client()
assert client1 is client2  # Same object by reference
```

---

## When to Use

Use `@cache.local()` when:

| Scenario | Example | Why |
|----------|---------|-----|
| **SDK connections** | Langfuse, httpx.Client, gRPC stubs | Maintain session state, reuse TCP connection |
| **ML models** | Loaded transformers, embedders | In-memory weight matrices, can't be serialized |
| **Database connections** | SQLAlchemy session, asyncpg connection | Stateful, must be re-created per process |
| **Language runtime objects** | Java objects via jpype, R objects via rpy2 | Opaque to Python, cross-language marshalling complex |

**NOT for**: Serializable data (dicts, dataframes, JSON). Use `@cache` for those.

---

## Quick Start

```python notest
from cachekit import cache

# Cache a connection pool (same instance reused)
@cache.local()
def get_database_client(host: str):
    return SQLAlchemy.create_engine(f"postgresql://{host}/mydb")

# First call: creates client
db1 = get_database_client("localhost")

# Second call with same args: returns cached instance
db2 = get_database_client("localhost")

assert db1 is db2  # Guaranteed same object
```

Configure TTL and size:

```python notest
@cache.local(ttl=600, max_entries=128)
def get_model(model_name: str):
    return transformers.AutoModel.from_pretrained(model_name)
```

---

## Mutation Warning ⚠️

**Critical**: Cached objects are returned by reference. Mutations affect all callers.

```python notest
@cache.local()
def get_config_dict():
    return {"timeout": 30, "retries": 3}

config1 = get_config_dict()
config1["timeout"] = 999  # Mutation!

config2 = get_config_dict()
print(config2["timeout"])  # Prints 999, not 30
```

**Fix**: Copy if you need to mutate:

```python notest
import copy

@cache.local()
def get_config_dict():
    return {"timeout": 30, "retries": 3}

config = copy.copy(get_config_dict())  # Shallow copy
config["timeout"] = 999  # Safe mutation
```

---

## Identity Semantics

**Same args = same object**:

```python notest
@cache.local()
def get_client(api_key: str):
    return APIClient(api_key=api_key)

client_a = get_client("key123")
client_b = get_client("key123")
assert client_a is client_b  # True (identity check)

client_c = get_client("key456")
assert client_a is not client_c  # False (different args)
```

**Feature for connection reuse**:
- Multiple callers within same process reuse same socket, session state, credentials
- Reduces memory overhead, avoids re-authentication

**Footgun for mutable data**:
- Mutations visible to all callers
- Use `@cache` (which serializes) if you need isolation
- Or manually copy on retrieval

---

## Object Lifecycle

Cached objects are held strongly until eviction:

```
Function call with args A
  ↓
[Check cache]
  ├─ Hit: Return cached object (reference count unchanged)
  └─ Miss: Call function, store result
  ↓
[Store in LRU cache (max_entries)]
  ↓
[LRU eviction when full]
  └─ Oldest/least-used entry removed
  ↓
[Object eligible for garbage collection if no other refs]
```

**Strong reference guarantee**: Cached object won't be garbage-collected until:
1. Evicted from cache (LRU), OR
2. Cache cleared via `cache_clear()`, OR
3. Function invalidated via `invalidate_cache()`

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ttl` | int | 300 | Time-to-live in seconds. Objects evicted after TTL expires. |
| `max_entries` | int | 256 | Maximum cache entries. Oldest removed when full. |
| `namespace` | str | None | Optional key namespace for multi-tenant isolation. |
| `key` | callable | None | Custom key function. Default: `(args, kwargs)` hash. |

---

## Wrapper API

All cachekit decorators expose the same cache management interface:

```python notest
@cache.local(ttl=300)
def get_client(api_key: str):
    return Client(api_key)

# Get cache statistics
info = get_client.cache_info()
print(info)  # CacheInfo(hits=10, misses=5, maxsize=256, currsize=3)

# Invalidate specific cache entry
get_client.invalidate_cache("my-api-key")

# Clear all cached entries
get_client.cache_clear()

# Access original function (bypasses cache)
raw_result = get_client.__wrapped__("my-api-key")

# Async variant
@cache.local()
async def get_async_client(api_key: str):
    return await AsyncClient(api_key)

await get_async_client.ainvalidate_cache("my-api-key")
```

---

## Comparison

| Feature | `@cache.local()` | `functools.lru_cache` | `cachetools.TTLCache` |
|---------|:----------------:|:---------------------:|:---------------------:|
| **In-process only** | ✅ | ✅ | ✅ |
| **Distributed** | ❌ | ❌ | ❌ |
| **TTL support** | ✅ | ❌ | ✅ |
| **Unhashable args** (dicts, lists) | ✅ | ❌ | ❌ |
| **Async functions** | ✅ | ❌ | ✅ |
| **Per-key invalidation** | ✅ | ❌ | ❌ |
| **Thread-safe** | ✅ | ✅ | ❌ (needs lock) |
| **Hit/miss statistics** | ✅ | ✅ | ❌ (size only) |

**Why @cache.local() wins**:
- Accepts unhashable args (no need to convert to strings)
- TTL + LRU (best of both worlds)
- Async-native, not a decorator shim
- Per-key invalidation without clearing entire cache
- Thread-safe by default

---

## Future: Lifecycle Callbacks (v0.7)

**Planned** (not yet available):

```python notest
def on_evict(key, value):
    """Called when cache entry is evicted."""
    value.cleanup()  # Close connection, free memory, etc.

@cache.local(on_evict=on_evict)
def get_connection():
    return Database.connect()
```

For now, manually call cleanup when needed:

```python notest
get_connection.cache_clear()  # All entries evicted, consider calling cleanup
```

---

## Examples

**ML model caching**:
```python notest
import torch
from cachekit import cache

@cache.local(ttl=3600, max_entries=4)
def load_model(model_name: str):
    """Load once, reuse across requests."""
    return torch.hub.load("pytorch/vision", model_name, pretrained=True)

# First inference: loads model (slow)
embeddings1 = load_model("resnet50")(image1)

# Second inference: reuses model (fast)
embeddings2 = load_model("resnet50")(image2)
```

**Database session pool**:
```python notest
from sqlalchemy.orm import Session
from cachekit import cache

@cache.local()
def get_session(db_url: str) -> Session:
    """Reuse SQLAlchemy session per connection string."""
    engine = sqlalchemy.create_engine(db_url)
    return Session(engine)

# Multiple queries reuse same session
result1 = get_session("postgresql://...").query(User).all()
result2 = get_session("postgresql://...").query(Product).all()
```

**Async HTTP client**:
```python notest
import httpx
from cachekit import cache

@cache.local()
async def get_http_client(api_key: str):
    """Reuse HTTP client for connection pooling."""
    return httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        http2=True
    )

# Both requests use same connection pool
response1 = await (await get_http_client("key123")).get("https://api.example.com/users")
response2 = await (await get_http_client("key123")).get("https://api.example.com/products")
```
