# Changelog

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
