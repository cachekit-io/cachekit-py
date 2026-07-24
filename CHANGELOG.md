# Changelog

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
