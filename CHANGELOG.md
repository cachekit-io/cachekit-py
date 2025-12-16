# Changelog

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
