**[Home](../README.md)** › **[Backends](README.md)** › **Google Cloud Storage Backend**

# Google Cloud Storage Backend

> Requires: `pip install cachekit[gcs]`

Store each cache entry as one object in a Google Cloud Storage bucket. Durable, shared across processes and machines, and needs no server to run: a fit for jobs and services that keep no local disk between runs and have no Redis.

## Basic Usage

```python notest
from cachekit import cache
from cachekit.backends.gcs import GCSBackend, GCSBackendConfig

backend = GCSBackend(GCSBackendConfig(bucket="my-cache-bucket"))

@cache(backend=backend, ttl=3600)
def cached_function():
    return expensive_computation()
```

Or select it from the environment, with no `backend=`:

```bash
export CACHEKIT_GCS_BUCKET="my-cache-bucket"
```

`CACHEKIT_GCS_BUCKET` is a backend selector like `CACHEKIT_REDIS_URL` or `CACHEKIT_FILE_CACHE_DIR`: set exactly one of them ([Backend Resolution Priority](README.md#backend-resolution-priority)).

## Configuration via Environment Variables

```bash
export CACHEKIT_GCS_BUCKET="my-cache-bucket"   # Required. A bucket name, not a gs:// URL
export CACHEKIT_GCS_PREFIX="cachekit/"         # Default: "" (objects at the bucket root)
export CACHEKIT_GCS_PROJECT="my-project"       # Default: none. Object reads and writes need no project
export CACHEKIT_GCS_TIMEOUT=10.0               # Default: 10.0 seconds (range: 0.1-600.0)
```

## Configuration via Python

Config objects need neither credentials nor the `gcs` extra:

```python
from cachekit.backends.gcs import GCSBackendConfig

config = GCSBackendConfig(
    bucket="my-cache-bucket",
    prefix="cachekit/",
    timeout=5.0,
)
```

`timeout` is how long to wait for a connection, and between bytes of a response. It is not a
deadline for the whole request.

## Authentication

The backend authenticates with [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) and has no credential settings of its own:

- **On Google Cloud** (Cloud Run, GKE, Compute Engine, Cloud Functions): the service account attached to the workload. Nothing to configure.
- **On your machine**: `gcloud auth application-default login`.

A missing credential raises an AUTHENTICATION `BackendError` when the backend is built. Under `@cache` with env selection, that degrades like any other backend failure: the function runs uncached.

## IAM

Grant the identity `roles/storage.objectUser` on the bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://my-cache-bucket \
  --member="serviceAccount:my-app@my-project.iam.gserviceaccount.com" \
  --role="roles/storage.objectUser"
```

That role is enough. The backend reads, writes, deletes and lists objects, and never reads the bucket's own metadata, so it needs no `storage.buckets.get`. The bucket must already exist: the backend does not create it.

If you write a custom role instead, it needs `storage.objects.get`, `storage.objects.create`, `storage.objects.delete` and `storage.objects.list`. Without `storage.objects.list`, GCS answers a read of an absent object with 403 instead of 404, so every cache miss raises an AUTHENTICATION `BackendError`. An identity that can only read (`roles/storage.objectViewer`) serves hits and misses; it cannot store entries or delete expired ones.

## Object Names and the Prefix

An entry's object name is the prefix followed by 32 hex characters, the BLAKE2b-128 hash of the cache key:

```
<prefix><blake2b-128 hex of the cache key>
cachekit/0e2a03b49262c15a063c04d5a29c0158
```

The prefix is used verbatim, so end it with `/` if you want a folder. It is a storage location only, like the file backend's `cache_dir`: it does not change the cache key, and two backends with different prefixes on one bucket never see each other's entries.

The object holds the bytes the [File backend](file.md) writes for the same key, value and expiry: the [file-backend format](https://github.com/cachekit-io/protocol/blob/main/spec/file-backend-format.md) of the CacheKit protocol, a 14-byte header that carries the expiry, then the payload. A cache directory copied into the bucket with `gcloud storage cp`, or a bucket prefix copied to a directory, stays readable from the other backend.

## Expiry and the Lifecycle Rule

Expiry lives in each object's header and is checked when the entry is read. An expired entry is a miss, and the `get` that finds it deletes it. An entry nobody reads again is never deleted by cachekit, so give the bucket a lifecycle rule as a backstop, with an `age` above your longest TTL (here 8 days, for TTLs of up to 7):

```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 8, "matchesPrefix": ["cachekit/"]}
    }
  ]
}
```

```bash
gcloud storage buckets update gs://my-cache-bucket --lifecycle-file=lifecycle.json
```

`age` is in days and counts from when the object was written. Drop `matchesPrefix` when the bucket holds nothing else.

> [!WARNING]
> The rule deletes by age alone. An entry cached with no TTL, or with a TTL longer than `age`, is deleted when it reaches `age` and becomes a miss.

## Requests per Operation

| Operation | Requests |
|-----------|----------|
| `get` | One download. A hit, a miss and an absent object each cost one request; an expired or corrupt entry costs a second, to delete it. |
| `set` | One upload that replaces the object: a single request up to 8 MiB, a resumable upload of several requests above it. The object is replaced when the upload completes. |
| `exists` | One ranged read of the 14-byte header. An empty object costs a second read, and a corrupt or expired one a delete. |
| `delete` | One delete. |
| `health_check()` | One listing of at most one object under the prefix. |

The backend retries no request. The one exception is in the credentials layer: after an HTTP 401 it refreshes the access token and sends the request again. A failed request raises a `BackendError`:

| Failure | `BackendErrorType` |
|---------|--------------------|
| HTTP 401, 403; credentials missing or refused | `AUTHENTICATION` |
| HTTP 429, 5xx; connection failed; token endpoint or metadata server unreachable; checksum mismatch | `TRANSIENT` |
| Timeout; HTTP 408 | `TIMEOUT` |
| Any other HTTP error, such as a bucket that does not exist | `PERMANENT` |

A 404 for an object is not an error: `get` returns `None` and `delete` returns `False`. Under `@cache`, a failed read is a miss and the function runs, and a failed write skips the backend only.

## When to Use

**Use GCSBackend when**:
- The workload runs on Cloud Run, Cloud Functions or batch jobs: no local disk that survives, no Redis
- The cached call is expensive (seconds, or money) and its result is worth keeping for hours or days
- Several machines should share results, and a round trip to GCS on each L1 miss is acceptable

**When NOT to use**:
- Hot paths that need low-latency reads: every L1 miss is an HTTPS request to GCS (use [Redis](redis.md) or [Memcached](memcached.md))
- You need distributed locking for stampede protection (use [Redis](redis.md) or [CacheKit Cloud](cachekitio.md))
- You need sliding expiration via `refresh_ttl_on_get` (see [Limitations](#limitations))
- Many small, short-lived entries: each write is a billed GCS operation

## Characteristics

- Latency: one HTTPS request to GCS per operation, which depends on your network and region ([not measured here](../performance.md#not-measured-here))
- TTL support: Yes, checked on read; no upper bound below the file format's 10 years
- Cross-process: Yes (shared by every process that can reach the bucket)
- Persistence: Yes
- Threads: threads may share one backend
- Fork: build the backend after `fork()`, not before

## Limitations

1. **No TTL inspection or refresh.** A GCS object cannot be edited in place, so refreshing a TTL would rewrite the whole entry. The backend is not a `TTLInspectableBackend`: `refresh_ttl_on_get` does not apply to it (the decorator warns once and serves the hit).
2. **No locking.** No distributed lock, so no cross-process stampede protection.
3. **Last writer wins.** Two processes that store the same key at once each upload a whole object, and the later upload replaces the earlier one. A reader sees one or the other, never a mix.
4. **Whole-function invalidation is per process.** `invalidate_cache()` with no arguments deletes only the keys the calling process knows ([Whole-Function Invalidation](../features/l1-invalidation.md#whole-function-invalidation)).
5. **No eviction or size cap.** The bucket grows until entries are read after they expire or the lifecycle rule deletes them.
6. **Encrypted entries are not bound to their location.** `@cache.secure` binds each entry to its cache key. The bucket and the prefix are not part of that binding, as with the file backend's directory.

## Testing Against an Emulator

The client honours `STORAGE_EMULATOR_HOST`, so the backend runs unchanged against [fake-gcs-server](https://github.com/fsouza/fake-gcs-server), with no credentials:

```bash
bash .github/scripts/start-fake-gcs.sh
STORAGE_EMULATOR_HOST=http://localhost:4443 uv run pytest tests/integration/test_gcs_integration.py
```

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [File Backend](file.md) — The same entry format on local disk
- [Redis Backend](redis.md) — Low-latency shared caching with locking support
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
