**[Home](README.md)** › **Quick Start**

# Quick Start

> **Get caching working in 5 minutes**

---

## 1. Install

```bash
pip install cachekit
# or
uv pip install cachekit
```

## 2. Set Up Redis

```bash
# Start Redis with Docker
docker run -d -p 6379:6379 redis

# Or use existing Redis
export REDIS_URL=redis://localhost:6379/0
```

## 3. Cache Your First Function

```python
from cachekit import cache

@cache
def expensive_function():
    print("Computing...")
    return sum(range(1000000))

# First call: computes (prints "Computing...")
result = expensive_function()

# Second call: instant from cache
result = expensive_function()
```

> [!TIP]
> That's it. You're caching.

---

## Common Patterns

<details>
<summary><strong>Cache with Expiration</strong></summary>

```python
@cache(ttl=3600)  # Expire after 1 hour
def get_user(user_id):
    return db.query(user_id)
```

</details>

<details>
<summary><strong>Async Functions</strong></summary>

```python
@cache()
async def fetch_data(user_id):
    return await api.get(f"/users/{user_id}")
```

</details>

<details>
<summary><strong>Secure Cache (Encrypted)</strong></summary>

```python notest
# First: set encryption key
import os
os.environ["CACHEKIT_MASTER_KEY"] = os.popen("openssl rand -hex 32").read()

# Then: use @cache.secure (master_key can also be passed explicitly)
@cache.secure(ttl=3600, master_key="a" * 64, backend=None)
def get_ssn(user_id):
    return db.get_ssn(user_id)  # Encrypted in Redis (illustrative - db not defined)
```

</details>

<details>
<summary><strong>Custom Namespace</strong></summary>

```python
@cache(ttl=1800, namespace="users")
def get_profile(user_id):
    return build_profile(user_id)

@cache(ttl=600, namespace="posts")
def get_feed(user_id):
    return fetch_feed(user_id)
```

</details>

---

## What's Next?

| Direction | Resource |
|:----------|:---------|
| **Next** | [Getting Started Guide](getting-started.md) - Progressive feature disclosure (30 min) |

### See Also

| Resource | Description |
|:---------|:------------|
| [API Reference](api-reference.md) | Complete decorator parameters |
| [Configuration Guide](configuration.md) | Environment variable setup |
| [Troubleshooting Guide](troubleshooting.md) | Common errors and solutions |
| [Backend Guide](guides/backend-guide.md) | Custom storage backends |

---

<div align="center">

*Last Updated: 2025-12-02*

</div>
