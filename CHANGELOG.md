# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 (2025-12-11)


### Features

* initial cachekit v0.1.0-alpha oss release ([a0800c3](https://github.com/cachekit-io/cachekit-py/commit/a0800c3869e29a9d3a3fd553ac32be3b6621e434))


### Bug Fixes

* backend=None uses L1 (in-memory) cache which works everywhere. ([7ce81e2](https://github.com/cachekit-io/cachekit-py/commit/7ce81e21d667cf47f935884fdd65adafa39581ba))


### Documentation

* add notest markers to error examples in rust-serialization.md ([c846983](https://github.com/cachekit-io/cachekit-py/commit/c84698390c04efde7abf5c3c1f2d26e1b30a5ad7))
* alpha warning ([14ba885](https://github.com/cachekit-io/cachekit-py/commit/14ba8850218154984be6ada20fae0246c438900c))
* document CACHEKIT_MASTER_KEY test fixture in CONTRIBUTING.md ([013e9f9](https://github.com/cachekit-io/cachekit-py/commit/013e9f9cf4179d5df5055b963b5cd08956d446b9))
* enable encryption examples by setting CACHEKIT_MASTER_KEY in test fixtures ([94b61d5](https://github.com/cachekit-io/cachekit-py/commit/94b61d5d058e49391de336a09468b5f44273de77))
* fix API drift in api-reference.md by enabling doc tests ([021ebb4](https://github.com/cachekit-io/cachekit-py/commit/021ebb42d20a1c82fe0ad1af859aa1a3141941dd))
* fix API drift in circuit-breaker.md by enabling doc tests ([a6eb44a](https://github.com/cachekit-io/cachekit-py/commit/a6eb44a87d630a21c4eb25c87ed0e63fb4c31b89))

## [0.1.0](https://github.com/cachekit-io/cachekit-py/releases/tag/v0.1.0) (2025-12-11)

### Features

* Initial release of cachekit
* Two-tier caching (L1 in-memory + L2 Redis)
* Zero-knowledge encryption with `@cache.secure`
* Circuit breaker for Redis fault tolerance
* Distributed locking to prevent cache stampedes
* Adaptive timeouts based on Redis latency
* MessagePack serialization with LZ4 compression
