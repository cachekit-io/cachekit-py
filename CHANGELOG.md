# Changelog

## [0.20.0](https://github.com/cachekit-io/cachekit-py/compare/v0.19.0...v0.20.0) (2026-09-26)


### ⚠ BREAKING CHANGES

* **decorators:** refuse encryption with backend=None — L1-only held plaintext (LAB-4665) ([#322](https://github.com/cachekit-io/cachekit-py/issues/322))
* **logging:** `cachekit.logging.JsonFormatter` is removed; importing it now raises ImportError. `StructuredLogger.circuit_breaker_state_change` and `StructuredLogger.set_correlation_id` are removed and now raise AttributeError. No deprecation aliases. Use `set_trace_id` / `clear_trace_id` (or `cachekit.monitoring.correlation_tracking`) for request correlation, and your own `logging.Formatter` subclass for JSON output.
* **reliability:** cachekit.reliability.create_optimized_decorator_config is renamed to cachekit.reliability.create_decorator_config. Update imports; no alias is provided. cachekit.hash_utils.fast_hash is renamed to blake3_hash (module-internal, never exported from the package).
* **keys:** cache keys for any serializer other than the default change identity on upgrade — no configuration change is required to be affected. A deployment using `serializer="auto"`, `"orjson"`, `"arrow"`, or any serializer passed as an instance was writing `:1s` keys and will now write `:1a`, `:1o`, `:1w` or an `x`-prefixed code. That function's entire working set recomputes once at deploy, so plan a cold cache or roll out behind existing warm-up / stampede controls. Deployments on the default serializer are unaffected: their keys were already `:1s` and stay `:1s`. Orphaned entries are also a retention question, not only a hit-rate one: once the key changes, `invalidate_cache()` computes the new key and can no longer reach the old copy, so a deletion for erasure, consent withdrawal or permission revocation reports success while the pre-upgrade entry survives to its TTL — or indefinitely where `ttl=None`. Flush the affected namespaces on upgrade if you cache personal data rather than relying on expiry.
* **config:** CachekitConfig drops retry_on_timeout, max_retries, retry_delay_ms, early_refresh_ratio, enable_corruption_detection and max_key_size, and no longer reads CACHEKIT_RETRY_ON_TIMEOUT, CACHEKIT_MAX_RETRIES, CACHEKIT_RETRY_DELAY_MS, CACHEKIT_EARLY_REFRESH_RATIO, CACHEKIT_ENABLE_CORRUPTION_DETECTION or CACHEKIT_MAX_KEY_SIZE. None of them changed behaviour. Passing one to CachekitConfig(...) now raises ValidationError; CachekitConfig ignores a still-exported env var, so startup is unaffected. CachekitIOBackendConfig declares its own max_retries under the same prefix and still parses CACHEKIT_MAX_RETRIES; this release does not change it. Integrity checking is set per decorator (integrity_checking=), refresh-ahead timing by L1CacheConfig.swr_threshold_ratio, and Memcached retries by MemcachedBackendConfig.retry_attempts.
* **decorators:** `CachekitIOBackend(...)` configuration errors (missing, empty or whitespace-containing API key; invalid API URL) now raise `cachekit.config.ConfigurationError` at construction instead of `ValueError` or pydantic `ValidationError`, so code catching `ValueError` there must catch `ConfigurationError`. `@cache.io(backend=...)` and `@cache.io(config=...)` now raise `ConfigurationError` instead of silently ignoring the argument. Under `set_default_backend()`, `@cache(config=...)` now uses the backend inside `config=` instead of the module default. `cachekit.backends.cachekitio.client.get_sync_http_client()` is replaced by `lease_sync_http_client()`, which returns a `SyncClientLease`: hold the lease for as long as its `.client` is used, because the client is closed when the lease is dropped.
* **encryption:** single-tenant tenant_id defaults to "default", not a deployment UUID (LAB-4666) ([#321](https://github.com/cachekit-io/cachekit-py/issues/321))

### Bug Fixes

* **cache_handler:** evict on corrupt CK frame header, not just payload (LAB-4075) ([#307](https://github.com/cachekit-io/cachekit-py/issues/307)) ([ed799c1](https://github.com/cachekit-io/cachekit-py/commit/ed799c133db435a39e87d363474bd23f5ff5624a))
* **decorators:** [@cache](https://github.com/cache).io accepts api_key= and rejects backend= (LAB-4643) ([#320](https://github.com/cachekit-io/cachekit-py/issues/320)) ([4d36167](https://github.com/cachekit-io/cachekit-py/commit/4d3616759cdf491ef46f9e103b47fa043e217d18))
* **decorators:** async miss-store records carry serializer/hit like sync (LAB-3755) ([#295](https://github.com/cachekit-io/cachekit-py/issues/295)) ([b733196](https://github.com/cachekit-io/cachekit-py/commit/b733196b3f918a436947daa4e86d5208ff021d9c))
* **decorators:** refuse encryption with backend=None — L1-only held plaintext (LAB-4665) ([#322](https://github.com/cachekit-io/cachekit-py/issues/322)) ([f5340f6](https://github.com/cachekit-io/cachekit-py/commit/f5340f65b40ff11ca8a918fa0d753bc58eee81f4))
* **encryption:** classify a non-string original_type header as corruption, not tamper (LAB-4350) ([#310](https://github.com/cachekit-io/cachekit-py/issues/310)) ([2403e7e](https://github.com/cachekit-io/cachekit-py/commit/2403e7ee437e6059c20469ca7237e0c0e2e22fbb))
* **encryption:** single-tenant tenant_id defaults to "default", not a deployment UUID (LAB-4666) ([#321](https://github.com/cachekit-io/cachekit-py/issues/321)) ([c5da48a](https://github.com/cachekit-io/cachekit-py/commit/c5da48a702840698546bcc1e369c1c475c7dcfa2))
* **keys:** put the real serializer identity in the cache key (LAB-4351) ([#311](https://github.com/cachekit-io/cachekit-py/issues/311)) ([ee65250](https://github.com/cachekit-io/cachekit-py/commit/ee65250b9d9f2d8842eb760c0197190bd0b5b0a0))
* **redis:** classify ClusterDownError as TRANSIENT (LAB-5327) ([#339](https://github.com/cachekit-io/cachekit-py/issues/339)) ([e93b693](https://github.com/cachekit-io/cachekit-py/commit/e93b693ef6bb93fd0db8b4167c66ad0223bb13db))
* **redis:** release a lock won after acquire_lock cancellation (LAB-3606) ([#293](https://github.com/cachekit-io/cachekit-py/issues/293)) ([4cfdc63](https://github.com/cachekit-io/cachekit-py/commit/4cfdc63a67991f117d75275feb57912ab861b4b7))


### Code Refactoring

* **config:** remove six CachekitConfig knobs nothing reads (LAB-4740) ([#324](https://github.com/cachekit-io/cachekit-py/issues/324)) ([27e1f95](https://github.com/cachekit-io/cachekit-py/commit/27e1f95dc727e1b2c03ebed8c4dfd42f7caa1ad2))
* **logging:** remove circuit_breaker_state_change, set_correlation_id and JsonFormatter (LAB-4638) ([#317](https://github.com/cachekit-io/cachekit-py/issues/317)) ([0a71361](https://github.com/cachekit-io/cachekit-py/commit/0a71361593a44f1df9ba7f7d14f73a1cf0795022))
* **reliability:** rename create_optimized_decorator_config and fast_hash (LAB-4619) ([#315](https://github.com/cachekit-io/cachekit-py/issues/315)) ([8d49e38](https://github.com/cachekit-io/cachekit-py/commit/8d49e38752b2c5e1a2c552b18b28f1f2d8eb4cd2))

## [0.19.0](https://github.com/cachekit-io/cachekit-py/compare/v0.18.0...v0.19.0) (2026-09-22)


### ⚠ BREAKING CHANGES

* **logging:** remove dead compat surface and Redis branding from StructuredLogger (LAB-4621) ([#316](https://github.com/cachekit-io/cachekit-py/issues/316))
* **logging:** `cachekit.logging.UltraOptimizedStructuredLogger` is now `StructuredLogger`; the `cachekit.logging.StructuredRedisLogger` alias is removed; `cachekit.monitoring.pool_monitor.OptimizedPoolMonitor` is now `PoolMonitor`. No deprecation aliases are provided. `get_structured_logger()` is unchanged and remains the supported entry point.
* **logging:** UltraOptimizedStructuredLogger.__init__ no longer accepts mask_sensitive; get_structured_logger() no longer accepts mask_sensitive and now keys _logger_instances on name alone; mask_sensitive_patterns is removed; ProfileConfig.mask_sensitive_data and ProfileConfig.lazy_pii_masking are removed. All were read by nothing and toggled no behavior. Constructors/callers passing them now raise TypeError instead of silently no-op'ing. Same removal shape as L1CacheConfig.namespace_index in v0.18.0 and L1CacheConfig.invalidation_enabled in v0.16.0 (LAB-520).

### Features

* **backend:** bound L1 backfill by the server's remaining freshness (LAB-557) ([#268](https://github.com/cachekit-io/cachekit-py/issues/268)) ([7bd5abf](https://github.com/cachekit-io/cachekit-py/commit/7bd5abf4ba838600b7ae333715a3575dc587143e))
* **concurrency:** free-threaded CPython support — memory-ordering fixes, gil_used=false, CI lane (LAB-511) ([#265](https://github.com/cachekit-io/cachekit-py/issues/265)) ([bda770b](https://github.com/cachekit-io/cachekit-py/commit/bda770bce822d9a6eff98e555c5f6fd92e509a9c))


### Bug Fixes

* **ci:** fail loudly on attestation lookup failure; decide the codecov pair (LAB-2528) ([#270](https://github.com/cachekit-io/cachekit-py/issues/270)) ([2a8b941](https://github.com/cachekit-io/cachekit-py/commit/2a8b941043e8bc3d9a143428495e13226aed4901))
* **ci:** make the Atheris fuzz job capable of failing + repair its dead targets (LAB-1140) ([#269](https://github.com/cachekit-io/cachekit-py/issues/269)) ([6ab0c28](https://github.com/cachekit-io/cachekit-py/commit/6ab0c289f8fe1e8891c30b94faed7a3501809e16))
* **decorators:** async get hits record serializer/size/hit like the sync path (LAB-3765) ([#297](https://github.com/cachekit-io/cachekit-py/issues/297)) ([9b96fd2](https://github.com/cachekit-io/cachekit-py/commit/9b96fd2b90364bd36ef3268bca6ff4df2daafe40))
* **decorators:** async lock double-check L2 hits record get telemetry (LAB-3769) ([#303](https://github.com/cachekit-io/cachekit-py/issues/303)) ([683b1c7](https://github.com/cachekit-io/cachekit-py/commit/683b1c7cc178948c922683551ae41e6c77beef24))
* **decorators:** honour set_default_backend() when called after decoration (LAB-4457) ([#313](https://github.com/cachekit-io/cachekit-py/issues/313)) ([d20204c](https://github.com/cachekit-io/cachekit-py/commit/d20204c4f16270e75ffa69e601995bdcc51f700c))
* **file:** guard eviction unlink against a concurrent rename (LAB-2685) ([#285](https://github.com/cachekit-io/cachekit-py/issues/285)) ([e917a57](https://github.com/cachekit-io/cachekit-py/commit/e917a57c4e6065eb161da8011a26d6805d49c327))
* **file:** write every byte or fail; evict a payload that shrank under read (LAB-2682) ([#272](https://github.com/cachekit-io/cachekit-py/issues/272)) ([068adb2](https://github.com/cachekit-io/cachekit-py/commit/068adb276c2b1f552b527e4ba91fc2904cd75be5))
* **logging:** redact raw cache keys on all log paths (LAB-304) ([#264](https://github.com/cachekit-io/cachekit-py/issues/264)) ([81f97fb](https://github.com/cachekit-io/cachekit-py/commit/81f97fb76245e3924bf91b61996e1b4e7639f702))
* **logging:** sanitise error kwarg at the structured cache-operation sinks (LAB-3666) ([#301](https://github.com/cachekit-io/cachekit-py/issues/301)) ([d27ec29](https://github.com/cachekit-io/cachekit-py/commit/d27ec29e0dbdfdf64e253a6fb5665b4d30639869))
* **redis:** stop lock waiters pinning executor threads (LAB-3596) ([#290](https://github.com/cachekit-io/cachekit-py/issues/290)) ([ddbeb91](https://github.com/cachekit-io/cachekit-py/commit/ddbeb910a9de7932ed93ebbcad4c8435278831e2))
* **serializers:** bound forged-entry error echoes; retire columnar dead code (LAB-3131) ([#289](https://github.com/cachekit-io/cachekit-py/issues/289)) ([10a1049](https://github.com/cachekit-io/cachekit-py/commit/10a10498d406bce7b4fd8dff6cb2e88e80f9b0a2))
* **serializers:** bound untrusted msgpack decode depth and header allocation (LAB-2503) ([#276](https://github.com/cachekit-io/cachekit-py/issues/276)) ([f7c087d](https://github.com/cachekit-io/cachekit-py/commit/f7c087dfbd7972bd711f32054949c1b739e00146))
* **serializers:** fail closed on unverified DataFrame/Series envelopes (LAB-2736) ([#304](https://github.com/cachekit-io/cachekit-py/issues/304)) ([60da54f](https://github.com/cachekit-io/cachekit-py/commit/60da54fd614717735f831c27831ed4cfe758ea35))
* **serializers:** take the format from the envelope, not the header; drop the header-gated fall-through (LAB-2736) ([#308](https://github.com/cachekit-io/cachekit-py/issues/308)) ([69db1c5](https://github.com/cachekit-io/cachekit-py/commit/69db1c5ef768195471c0e971965c73e77d2da9cd))
* **serializers:** type ByteStorage.retrieve failures and collapse duplicated columnar decode (LAB-2736) ([#287](https://github.com/cachekit-io/cachekit-py/issues/287)) ([0aa78ca](https://github.com/cachekit-io/cachekit-py/commit/0aa78ca8dc0749492e6078cdcfe2330743bb202f))


### Performance Improvements

* **decorators:** backfill L1 on sync L2 hits; size stats by envelope length (LAB-348) ([#294](https://github.com/cachekit-io/cachekit-py/issues/294)) ([c019b26](https://github.com/cachekit-io/cachekit-py/commit/c019b2659eb964408facb6d7191b85a26177342a))


### Code Refactoring

* **logging:** remove dead compat surface and Redis branding from StructuredLogger (LAB-4621) ([#316](https://github.com/cachekit-io/cachekit-py/issues/316)) ([98d616e](https://github.com/cachekit-io/cachekit-py/commit/98d616e6e5f5aec79ba7fe285909f662e8722c00))
* **logging:** remove dead PII-masking knobs (LAB-3797) ([#300](https://github.com/cachekit-io/cachekit-py/issues/300)) ([705640b](https://github.com/cachekit-io/cachekit-py/commit/705640bde14b73c249ed6d8b30733eb46752a9a2))
* **logging:** rename UltraOptimizedStructuredLogger to StructuredLogger, OptimizedPoolMonitor to PoolMonitor (LAB-4617) ([#314](https://github.com/cachekit-io/cachekit-py/issues/314)) ([2f7c979](https://github.com/cachekit-io/cachekit-py/commit/2f7c9799d5f9aa46db4134febb6710b72c15d457))

## [0.18.0](https://github.com/cachekit-io/cachekit-py/compare/v0.17.1...v0.18.0) (2026-09-03)


### ⚠ BREAKING CHANGES

* L1CacheConfig.namespace_index is removed. The flag was read by nothing and toggled no behavior, but it shipped in v0.17.1 and docs/configuration.md documented a copy-pasteable L1CacheConfig(..., namespace_index=True) example — L1CacheConfig is a frozen dataclass, so constructors still passing it now raise TypeError instead of silently lying. L1Cache.invalidate_by_key(), .invalidate_by_namespace() and .invalidate_all() are removed with it; per-key L1Cache.invalidate() is unaffected. Same removal shape as L1CacheConfig.invalidation_enabled in v0.16.0 (LAB-520).

### Features

* **encryption:** keyring rotation — previous_master_keys + fingerprint selection (LAB-684) ([#261](https://github.com/cachekit-io/cachekit-py/issues/261)) ([e1b05ce](https://github.com/cachekit-io/cachekit-py/commit/e1b05ce1b5f30c63d86ab0cab6f4ddbc9af8cef6))


### Bug Fixes

* **cachekitio:** percent-encode cache key in request path (LAB-2846) ([#279](https://github.com/cachekit-io/cachekit-py/issues/279)) ([f000ba3](https://github.com/cachekit-io/cachekit-py/commit/f000ba340f84d435f04fd19ba453bb60385e9dd0))
* **fuzz:** commit per-target corpus seeds cargo-fuzz actually loads (LAB-1149) ([#263](https://github.com/cachekit-io/cachekit-py/issues/263)) ([1b85f56](https://github.com/cachekit-io/cachekit-py/commit/1b85f5605b532bf001d3756b86a9a11a3e1ba705))
* **l1:** delete dead backed-mode SWR machinery; docs stop claiming backed SWR (LAB-388) ([#256](https://github.com/cachekit-io/cachekit-py/issues/256)) ([878ad08](https://github.com/cachekit-io/cachekit-py/commit/878ad0860a340477257d2ac4f19f3f107f0789b4))


### Performance Improvements

* **file:** eliminate two full-payload copies on the non-mmap read path (LAB-770) ([#267](https://github.com/cachekit-io/cachekit-py/issues/267)) ([f7e236b](https://github.com/cachekit-io/cachekit-py/commit/f7e236b522010f0b18233eed91f3655629f128d0))


### Code Refactoring

* delete dead L1 namespace-index/bulk-invalidation machinery (LAB-1433) ([#258](https://github.com/cachekit-io/cachekit-py/issues/258)) ([2607faf](https://github.com/cachekit-io/cachekit-py/commit/2607fafd3d0dcd974e94788b7e5d46ee676701d4))

## [0.17.1](https://github.com/cachekit-io/cachekit-py/compare/v0.17.0...v0.17.1) (2026-07-29)


### Bug Fixes

* **fuzz:** fuzz the codec that ships — core 0.4.0, all 14 targets, fail loudly (LAB-1136) ([#251](https://github.com/cachekit-io/cachekit-py/issues/251)) ([511b1e5](https://github.com/cachekit-io/cachekit-py/commit/511b1e520dc84a6722c5903e519d407d1647f408))

## [0.17.0](https://github.com/cachekit-io/cachekit-py/compare/v0.16.0...v0.17.0) (2026-07-29)


### Features

* **envelope:** pick up cachekit-core 0.4.0 bin envelopes (LAB-900) ([#249](https://github.com/cachekit-io/cachekit-py/issues/249)) ([fa9ea36](https://github.com/cachekit-io/cachekit-py/commit/fa9ea36ce0b3d8e0bd15166d9207f87b9ebb91d9))


### Bug Fixes

* **serializers:** byte-aware Arrow batch sizing holds the memory bound on skewed frames (LAB-110) ([#244](https://github.com/cachekit-io/cachekit-py/issues/244)) ([e67db58](https://github.com/cachekit-io/cachekit-py/commit/e67db58c5c8e2d99bea6514b9066e76404466789))


### Performance Improvements

* **arrow:** stream serialize-to-backend writes via BufferWritableBackend (LAB-766) ([#247](https://github.com/cachekit-io/cachekit-py/issues/247)) ([539fde9](https://github.com/cachekit-io/cachekit-py/commit/539fde98c0f954efd03b5f25a36b676966789a2d))

## [0.16.0](https://github.com/cachekit-io/cachekit-py/compare/v0.15.0...v0.16.0) (2026-07-24)


### ⚠ BREAKING CHANGES

* L1CacheConfig.invalidation_enabled is removed. The flag was read by nothing and toggled no behavior; constructors passing it now raise TypeError instead of silently lying.

### Bug Fixes

* **security:** EncryptionWrapper.deserialize fails closed on plaintext-claiming input (LAB-271) ([#242](https://github.com/cachekit-io/cachekit-py/issues/242)) ([6fcb115](https://github.com/cachekit-io/cachekit-py/commit/6fcb115d19d924cd7676b371fdc2e2afd2186424))


### Code Refactoring

* remove unwired cross-instance invalidation package (LAB-520) ([#237](https://github.com/cachekit-io/cachekit-py/issues/237)) ([c56ac0a](https://github.com/cachekit-io/cachekit-py/commit/c56ac0a0987eb8f3e4925034765431d060794277))

## [0.15.0](https://github.com/cachekit-io/cachekit-py/compare/v0.14.0...v0.15.0) (2026-07-23)


### ⚠ BREAKING CHANGES

* the no-op `adaptive_timeout` decorator kwarg, the `TimeoutConfig` nested config, `DecoratorConfig.timeout`, and `ProfileConfig.adaptive_timeout` are removed; `CircuitBreaker.call()` and `.call_async()` are removed (use should_allow_request() + record_success() / record_failure()). All were non-functional or dead in production.

### Code Refactoring

* remove non-functional adaptive timeout, dead CircuitBreaker.call, unused tenacity (LAB-522) ([#239](https://github.com/cachekit-io/cachekit-py/issues/239)) ([5ade101](https://github.com/cachekit-io/cachekit-py/commit/5ade10126f590f4469d99874beea6ecc6fdfab68))

## [0.14.0](https://github.com/cachekit-io/cachekit-py/compare/v0.13.0...v0.14.0) (2026-07-22)


### Features

* **cache:** stale-while-revalidate for cache.io — client transport + decorator surface (LAB-381) ([#228](https://github.com/cachekit-io/cachekit-py/issues/228)) ([7841ff0](https://github.com/cachekit-io/cachekit-py/commit/7841ff0d26870399c7e0fe2ddd62922d4f458280))


### Bug Fixes

* **decorators:** bind session counters to function identity, not wrapper instance ([#233](https://github.com/cachekit-io/cachekit-py/issues/233)) ([83b7c92](https://github.com/cachekit-io/cachekit-py/commit/83b7c9298d07f55ed893b7d8bbac0b17a7da00c5))
* **swr:** LAB-381 panel fast-follow — contextvar propagation, CWE-532 redaction, orphan cut ([#235](https://github.com/cachekit-io/cachekit-py/issues/235)) ([9e276b6](https://github.com/cachekit-io/cachekit-py/commit/9e276b67f82742ad94f576321ae7bb7695214dfd))

## [0.13.0](https://github.com/cachekit-io/cachekit-py/compare/v0.12.0...v0.13.0) (2026-07-21)


### Features

* **interop:** opt-in interop/v1 mode — cross-SDK keys + plain-MessagePack values (LAB-245) ([#220](https://github.com/cachekit-io/cachekit-py/issues/220)) ([e77fa6d](https://github.com/cachekit-io/cachekit-py/commit/e77fa6dc80649d93e990b681ba977bcdb6a4f01a))
* **security:** tamper telemetry + fail-closed decrypt policy (LAB-108) ([#218](https://github.com/cachekit-io/cachekit-py/issues/218)) ([52d6a46](https://github.com/cachekit-io/cachekit-py/commit/52d6a465ed081e23ef162a59352b12df855f6c2c))


### Bug Fixes

* address coderabbit review — run async interop guard before L1 lookup ([#227](https://github.com/cachekit-io/cachekit-py/issues/227)) ([1ae680f](https://github.com/cachekit-io/cachekit-py/commit/1ae680fd87561c82f34b6f2b4088c9ec111fbec6))
* **security:** route async decorator L2 reads through get_cached_value_async (LAB-111) ([#216](https://github.com/cachekit-io/cachekit-py/issues/216)) ([7df5860](https://github.com/cachekit-io/cachekit-py/commit/7df586059d6190ca847a8b18c1d30dc0a20a5a5f)), closes [#159](https://github.com/cachekit-io/cachekit-py/issues/159)

## [0.12.0](https://github.com/cachekit-io/cachekit-py/compare/v0.11.1...v0.12.0) (2026-07-20)


### ⚠ BREAKING CHANGES

* **config:** CachekitConfig fields enable_compression, compression_level and max_chunk_size_mb were removed; constructing CachekitConfig with them now raises a ValidationError (their CACHEKIT_* env vars were already no-ops and remain ignored). max_value_size is now enforced: serialized envelopes larger than it (default 100MB, CACHEKIT_MAX_VALUE_SIZE) are no longer cached.

### Features

* checksum FFI binding + benchmark (cachekit-core[#13](https://github.com/cachekit-io/cachekit-py/issues/13) Phase 2) ([#212](https://github.com/cachekit-io/cachekit-py/issues/212)) ([9245c30](https://github.com/cachekit-io/cachekit-py/commit/9245c300ad8d1a510592f991d3de0b63bcdf9795))


### Bug Fixes

* **config:** enforce l1_max_size_mb + max_value_size, remove dead zlib/chunk knobs (LAB-109) ([#217](https://github.com/cachekit-io/cachekit-py/issues/217)) ([a5a347c](https://github.com/cachekit-io/cachekit-py/commit/a5a347c4334f017e88f366ee32a3b762a1946f93))
* **decorators:** L1-only mode (backend=None) honors L1CacheConfig SWR + max_size_mb (LAB-106) ([#219](https://github.com/cachekit-io/cachekit-py/issues/219)) ([bef4c05](https://github.com/cachekit-io/cachekit-py/commit/bef4c05bb3442361fe6ac166ebbd4ddd966a6b3e))
* **redis:** honour redis_url, fix zero-config DI crash, add finite socket timeouts (LAB-352) ([#224](https://github.com/cachekit-io/cachekit-py/issues/224)) ([e45106e](https://github.com/cachekit-io/cachekit-py/commit/e45106e0ffd832f01280b123f249692ed9a3c7db))
* **security:** fail closed on plaintext frames when encryption is enabled (LAB-241) ([#215](https://github.com/cachekit-io/cachekit-py/issues/215)) ([7e5be4a](https://github.com/cachekit-io/cachekit-py/commit/7e5be4aee64d1a24b9e7278e5c628e3115154e45))
* **serializers:** AutoSerializer sets compressed metadata from the actual ByteStorage codec ([#211](https://github.com/cachekit-io/cachekit-py/issues/211)) ([47f8520](https://github.com/cachekit-io/cachekit-py/commit/47f85205bc0ea60ec6df4bd9a1e2945887a390a5))


### Performance Improvements

* release the GIL during ByteStorage compress/hash (LAB-347) ([#223](https://github.com/cachekit-io/cachekit-py/issues/223)) ([269aecf](https://github.com/cachekit-io/cachekit-py/commit/269aecf6d6b3b8993b0f5b9aa1f5c1f527b578ff))

## [0.11.1](https://github.com/cachekit-io/cachekit-py/compare/v0.11.0...v0.11.1) (2026-06-26)


### Bug Fixes

* re-read settings when CACHEKIT_MASTER_KEY appears + correct missing-key env var name ([#200](https://github.com/cachekit-io/cachekit-py/issues/200)) ([12c9fff](https://github.com/cachekit-io/cachekit-py/commit/12c9fffc5ac9249640610050160b1fa03501960d))

## [0.11.0](https://github.com/cachekit-io/cachekit-py/compare/v0.10.1...v0.11.0) (2026-06-20)


### ⚠ BREAKING CHANGES

* orjson is no longer installed by `pip install cachekit`. To use the orjson serializer (serializer="orjson" or OrjsonSerializer), install `cachekit[json]`. Without it, get_serializer("orjson") raises an ImportError with an actionable install hint.

### Features

* make orjson an optional dependency (cachekit[json]) ([#196](https://github.com/cachekit-io/cachekit-py/issues/196)) ([4512a2a](https://github.com/cachekit-io/cachekit-py/commit/4512a2a19240a38a2dd4eabf401ef4cbd4722be4))


### Security

* bump msgpack floor to 1.2.1 (GHSA-6v7p-g79w-8964) ([#197](https://github.com/cachekit-io/cachekit-py/issues/197)) ([fc1bb80](https://github.com/cachekit-io/cachekit-py/commit/fc1bb80d6f3fb02854b8447eac4043e99769013c))

## [0.10.1](https://github.com/cachekit-io/cachekit-py/compare/v0.10.0...v0.10.1) (2026-06-19)


### Bug Fixes

* canonicalize the pythonic serializer alias to auto ([#192](https://github.com/cachekit-io/cachekit-py/issues/192)) ([666d09c](https://github.com/cachekit-io/cachekit-py/commit/666d09c760bda72857ad1d41b2db84e71c905574))
* checksum-protect numpy serialization without compression overhead ([#189](https://github.com/cachekit-io/cachekit-py/issues/189)) ([3b0565f](https://github.com/cachekit-io/cachekit-py/commit/3b0565fbeb858dd96ed94932e771d4b4a83e2d2b))
* return writable numpy/Series reads and surface DataFrame/Series corruption clearly ([#191](https://github.com/cachekit-io/cachekit-py/issues/191)) ([46a3c3c](https://github.com/cachekit-io/cachekit-py/commit/46a3c3cdbb564377b3957fe6f8b2aae753d6def7))

## [0.10.0](https://github.com/cachekit-io/cachekit-py/compare/v0.9.1...v0.10.0) (2026-06-18)


### Features

* zero-copy mmap read path for large plaintext Arrow on the File backend ([#171](https://github.com/cachekit-io/cachekit-py/issues/171)) ([#187](https://github.com/cachekit-io/cachekit-py/issues/187)) ([1105454](https://github.com/cachekit-io/cachekit-py/commit/1105454381b7d1def92cffb780b8490286876dbf))


### Performance Improvements

* unwrap returns a zero-copy memoryview instead of copying the payload ([#162](https://github.com/cachekit-io/cachekit-py/issues/162)) ([#184](https://github.com/cachekit-io/cachekit-py/issues/184)) ([0901732](https://github.com/cachekit-io/cachekit-py/commit/0901732098f8f712f06b9866915f1563068e4c21))

## [0.9.1](https://github.com/cachekit-io/cachekit-py/compare/v0.9.0...v0.9.1) (2026-06-15)


### Bug Fixes

* **cachekitio:** surface HTTP 413 as a clear permanent "value too large" error ([#182](https://github.com/cachekit-io/cachekit-py/issues/182)) ([d76d526](https://github.com/cachekit-io/cachekit-py/commit/d76d5263699cf892b20d0fdeb63964466fda9b55))
* **deps:** bump pyo3 to 0.29 to clear RUSTSEC-2026-0176 and -0177 ([#183](https://github.com/cachekit-io/cachekit-py/issues/183)) ([beffbfc](https://github.com/cachekit-io/cachekit-py/commit/beffbfc1326c233549590a7b0cafb09913fd3980))


### Security

* send lock_id via X-CacheKit-Lock-Id header, not query string ([#131](https://github.com/cachekit-io/cachekit-py/issues/131)) ([#179](https://github.com/cachekit-io/cachekit-py/issues/179)) ([4cb00df](https://github.com/cachekit-io/cachekit-py/commit/4cb00dfe06bdba95c27386c3195e5819b21ab2a9))

## [0.9.0](https://github.com/cachekit-io/cachekit-py/compare/v0.8.0...v0.9.0) (2026-06-11)


### Features

* drop Python 3.9 support, require &gt;=3.10 ([#148](https://github.com/cachekit-io/cachekit-py/issues/148)) ([1bb9953](https://github.com/cachekit-io/cachekit-py/commit/1bb9953c0c275b5859c75b64e3306f31169a23df))
* honor user serializer under encryption via cross_sdk_compatible marker ([#153](https://github.com/cachekit-io/cachekit-py/issues/153)) ([2ad219d](https://github.com/cachekit-io/cachekit-py/commit/2ad219d290e8df22c4898c6b31bbcb650c8bb959)), closes [#134](https://github.com/cachekit-io/cachekit-py/issues/134)
* support explicit per-function encryption opt-out (tri-state) ([#151](https://github.com/cachekit-io/cachekit-py/issues/151)) ([bf86c43](https://github.com/cachekit-io/cachekit-py/commit/bf86c4374bdc9dae2982dac676a0c2d939fe490d))


### Bug Fixes

* bound memory for large DataFrame/Arrow caching (was OOMing at real sizes) ([#152](https://github.com/cachekit-io/cachekit-py/issues/152)) ([ccd32c5](https://github.com/cachekit-io/cachekit-py/commit/ccd32c5e6ef7e55f76a15066f3b7ec93931f7fe5))
* evict poisoned L2 entry on corruption at the read API ([#177](https://github.com/cachekit-io/cachekit-py/issues/177)) ([3a538fa](https://github.com/cachekit-io/cachekit-py/commit/3a538fa9a9dc77a5de1343a162bba8737131210e)), closes [#159](https://github.com/cachekit-io/cachekit-py/issues/159)
* handle pandas nullable/extension dtypes in no-pyarrow DataFrame fallback ([#176](https://github.com/cachekit-io/cachekit-py/issues/176)) ([4de3608](https://github.com/cachekit-io/cachekit-py/commit/4de36084ec40ee925da8c9e8ee414af98d7349c4))
* harden cache-envelope framing and compression config resolution ([#172](https://github.com/cachekit-io/cachekit-py/issues/172)) ([d079f79](https://github.com/cachekit-io/cachekit-py/commit/d079f7931c4610b6110b7d4dc512f009835f5fb0))
* reject non-finite (NaN/inf) TTL to prevent immortal cache entries ([#174](https://github.com/cachekit-io/cachekit-py/issues/174)) ([d90958c](https://github.com/cachekit-io/cachekit-py/commit/d90958c9aa0059f1f01600201b1668fac5097e27))
* stop legacy RedisBackend corrupting binary payloads ([#173](https://github.com/cachekit-io/cachekit-py/issues/173)) ([82d0417](https://github.com/cachekit-io/cachekit-py/commit/82d0417e2b40a9cf9e805173f7b822a18219e8c9))

## [0.8.0](https://github.com/cachekit-io/cachekit-py/compare/v0.7.0...v0.8.0) (2026-05-31)


### Features

* auto-detect memcached and file backends from environment ([#139](https://github.com/cachekit-io/cachekit-py/issues/139)) ([3f69e92](https://github.com/cachekit-io/cachekit-py/commit/3f69e921cfb00e12c18a23ad847032156434739e))


### Bug Fixes

* pass bare cache key to LockableBackend.acquire_lock ([#135](https://github.com/cachekit-io/cachekit-py/issues/135)) ([4d880b7](https://github.com/cachekit-io/cachekit-py/commit/4d880b7d4d12468c9863f3b2b98133b3d2380d3a))

## [0.7.0](https://github.com/cachekit-io/cachekit-py/compare/v0.6.1...v0.7.0) (2026-05-28)


### Features

* add tuple preservation to AutoSerializer and 'pythonic' alias ([#121](https://github.com/cachekit-io/cachekit-py/issues/121)) ([4ab03d8](https://github.com/cachekit-io/cachekit-py/commit/4ab03d82011c60b89329686caa9e59bb7d11a659))


### Bug Fixes

* 5 issues from encrypted-payload E2E testing ([#127](https://github.com/cachekit-io/cachekit-py/issues/127)) ([b1aab22](https://github.com/cachekit-io/cachekit-py/commit/b1aab222416efbe7e50d38497528b98874766690))
* conform CachekitIOBackend.acquire_lock to LockableBackend protocol ([#130](https://github.com/cachekit-io/cachekit-py/issues/130)) ([835d20b](https://github.com/cachekit-io/cachekit-py/commit/835d20b98221ec69f6a7fdc83ccbe3853ca773ee))
* preserve Python types in L1-only mode, allow cache_clear() on async ([#117](https://github.com/cachekit-io/cachekit-py/issues/117)) ([1fc506b](https://github.com/cachekit-io/cachekit-py/commit/1fc506bf517cb98b7674e081096a588f7cccec59))

## [0.6.1](https://github.com/cachekit-io/cachekit-py/compare/v0.6.0...v0.6.1) (2026-05-16)


### Bug Fixes

* invalidate_cache() with no args now clears all entries ([#108](https://github.com/cachekit-io/cachekit-py/issues/108)) ([f0dca32](https://github.com/cachekit-io/cachekit-py/commit/f0dca326325391eb878cfe8459aac5d4cfe15fcf))

## [0.6.0](https://github.com/cachekit-io/cachekit-py/compare/v0.5.1...v0.6.0) (2026-04-28)


### Features

* add [@cache](https://github.com/cache).local() for in-process reference caching ([#96](https://github.com/cachekit-io/cachekit-py/issues/96)) ([e5759f5](https://github.com/cachekit-io/cachekit-py/commit/e5759f524e5bf79b4fe39c7c219dbe7139061377))


### Bug Fixes

* use local&gt; preset ref for private renovate-config repo ([#99](https://github.com/cachekit-io/cachekit-py/issues/99)) ([bc30e7e](https://github.com/cachekit-io/cachekit-py/commit/bc30e7eef1164e4b7c935fbd4846e60538fb56d5))

## [0.5.1](https://github.com/cachekit-io/cachekit-py/compare/v0.5.0...v0.5.1) (2026-03-28)


### Bug Fixes

* fall back to CACHEKIT_MASTER_KEY env var in cache.secure decorator ([#79](https://github.com/cachekit-io/cachekit-py/issues/79)) ([7275e70](https://github.com/cachekit-io/cachekit-py/commit/7275e70f3face381270d41902f199ebc1690a1bf))

## [0.5.0](https://github.com/cachekit-io/cachekit-py/compare/v0.4.1...v0.5.0) (2026-03-27)


### Features

* add Memcached backend with pymemcache HashClient ([#67](https://github.com/cachekit-io/cachekit-py/issues/67)) ([a06e88a](https://github.com/cachekit-io/cachekit-py/commit/a06e88ad6fdd38b7e5ec11b93572da94e623a59d))

## [0.4.1](https://github.com/cachekit-io/cachekit-py/compare/v0.4.0...v0.4.1) (2026-03-23)


### Bug Fixes

* default L1-Status header for standalone CachekitIO usage ([#64](https://github.com/cachekit-io/cachekit-py/issues/64)) ([1eed9e4](https://github.com/cachekit-io/cachekit-py/commit/1eed9e4988eca248ae26e0061334fc0900bf5078))

## [0.4.0](https://github.com/cachekit-io/cachekit-py/compare/v0.3.1...v0.4.0) (2026-03-17)


### Features

* add CachekitIO SaaS backend with SSRF protection ([#60](https://github.com/cachekit-io/cachekit-py/issues/60)) ([472623e](https://github.com/cachekit-io/cachekit-py/commit/472623eed699e91eb1bfd1fc6356a76e2bc8605c))

## [0.3.1](https://github.com/cachekit-io/cachekit-py/compare/v0.3.0...v0.3.1) (2026-02-22)


### Bug Fixes

* async cache_clear() TypeError + nested numpy serialization ([#54](https://github.com/cachekit-io/cachekit-py/issues/54)) ([5a9c8ca](https://github.com/cachekit-io/cachekit-py/commit/5a9c8ca2f32bf56a6d222fba40fe99d0ebd3ebaf)), closes [#50](https://github.com/cachekit-io/cachekit-py/issues/50)
* lazy-load ArrowSerializer to avoid ImportError without pyarrow ([#44](https://github.com/cachekit-io/cachekit-py/issues/44)) ([065eb23](https://github.com/cachekit-io/cachekit-py/commit/065eb23a96957e01aa12b5b7f0bb4e8b42962467))

## [0.3.0](https://github.com/cachekit-io/cachekit-py/compare/v0.2.3...v0.3.0) (2025-12-18)


### Features

* extend key generator with Path, UUID, Decimal, Enum, datetime, and constrained numpy support ([#38](https://github.com/cachekit-io/cachekit-py/issues/38)) ([78e025a](https://github.com/cachekit-io/cachekit-py/commit/78e025a58658a89bac02634358422336ab5c3f2b))

## [0.2.3](https://github.com/cachekit-io/cachekit-py/compare/v0.2.2...v0.2.3) (2025-12-18)


### Bug Fixes

* downgrade to edition 2021 and MSRV 1.80 for stable Rust compatibility ([#32](https://github.com/cachekit-io/cachekit-py/issues/32)) ([9b13722](https://github.com/cachekit-io/cachekit-py/commit/9b137222f151208809d949e3e82757d8168bfc33))
* use AliasChoices for REDIS_URL env var fallback ([#35](https://github.com/cachekit-io/cachekit-py/issues/35)) ([274a925](https://github.com/cachekit-io/cachekit-py/commit/274a925848c4e4afd1c5b9cf02a87740e0b165da))

## [0.2.2](https://github.com/cachekit-io/cachekit-py/compare/v0.2.1...v0.2.2) (2025-12-18)


### Bug Fixes

* fail-fast import guard for ArrowSerializer when pyarrow missing ([#29](https://github.com/cachekit-io/cachekit-py/issues/29)) ([e6ab19e](https://github.com/cachekit-io/cachekit-py/commit/e6ab19ef63f8666d7f3726093ab40989533ddb93))

## [0.2.1](https://github.com/cachekit-io/cachekit-py/compare/v0.2.0...v0.2.1) (2025-12-17)


### Bug Fixes

* resolve async implementation bugs and race conditions ([#26](https://github.com/cachekit-io/cachekit-py/issues/26)) ([f75477a](https://github.com/cachekit-io/cachekit-py/commit/f75477ae16b651a1141649023b6592876ba7f120))

## [0.2.0](https://github.com/cachekit-io/cachekit-py/compare/v0.1.0...v0.2.0) (2025-12-16)


### Features

* add FileBackend for filesystem-based caching ([#18](https://github.com/cachekit-io/cachekit-py/issues/18)) ([e73b865](https://github.com/cachekit-io/cachekit-py/commit/e73b86567ea7f0a1cdaa87203aa5d16e5cc87778))

## 0.1.0 (2025-12-11)


### Features

* initial cachekit v0.1.0-alpha oss release ([a0800c3](https://github.com/cachekit-io/cachekit-py/commit/a0800c3869e29a9d3a3fd553ac32be3b6621e434))


### Bug Fixes

* backend=None uses L1 (in-memory) cache which works everywhere. ([7ce81e2](https://github.com/cachekit-io/cachekit-py/commit/7ce81e21d667cf47f935884fdd65adafa39581ba))
* L1-only mode (backend=None) should not attempt Redis connection ([0898986](https://github.com/cachekit-io/cachekit-py/commit/089898659d026a6b15fd8e09e05f6ef77f5e8e66))

## Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
