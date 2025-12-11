# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0](https://github.com/cachekit-io/cachekit-py/releases/tag/v0.1.0) (2025-12-11)

### Features

* Initial release of cachekit
* Two-tier caching (L1 in-memory + L2 Redis)
* Zero-knowledge encryption with `@cache.secure`
* Circuit breaker for Redis fault tolerance
* Distributed locking to prevent cache stampedes
* Adaptive timeouts based on Redis latency
* MessagePack serialization with LZ4 compression
