//! `PyO3` bindings for `cachekit-core`
//!
//! This crate provides thin Python wrappers around the cachekit-core library.
//! Business logic lives in cachekit-core, including the untrusted msgpack decode bound
//! (`cachekit_core::check_msgpack_structure`).
//!
//! The crate is safe Rust: unsafe code belongs in cachekit-core, whose CI verifies it, so a build
//! rejects any here rather than leaving it to the post-merge cargo-geiger count (SECURITY.md).
#![forbid(unsafe_code)]

// Re-export core types for use in Python bindings
pub use cachekit_core::{ByteStorage, StorageEnvelope};

#[cfg(feature = "encryption")]
pub use cachekit_core::{
    derive_domain_key,
    encryption::key_derivation::{derive_tenant_keys, key_fingerprint, TenantKeys},
    EncryptionError, Keyring, ZeroKnowledgeEncryptor,
};

// Python bindings (gated behind python feature)
#[cfg(feature = "python")]
pub mod python_bindings;

// The decorator's lock-free L1-hit counter. Not gated: its Python class is, so the counter's
// own tests run in the pure-Rust test build too.
pub mod hit_counter;

// The decorator's memoised L1-only hit, answered without a lock (decorators/wrapper.py).
#[cfg(feature = "python")]
pub mod l1_hit;

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Python module definition - exports raw byte storage and encryption
///
/// `gil_used = false` (the PyO3 0.28+ default, made explicit): declares the
/// module thread-safe under free-threaded CPython so importing it does not
/// force the GIL back on. Verified by audit: every `#[pyclass]`
/// exposes only `&self` methods except `Keyring.for_tenant`, whose `&mut self` borrow
/// PyO3 checks at run time (a concurrent call raises, never races), and in the features enabled here
/// cachekit-core's only shared state is its `AtomicU64` nonce counters (its
/// `Mutex` handle registry is behind the `ffi` feature, which is off). `l1_hit`'s
/// shared state is atomics (an entry's liveness and recency), a `OnceLock` (the
/// clock's zero), the tests' clock override (a `Mutex` with an atomic flag beside
/// it, held only to copy or swap the reference), one `Mutex` per entry (its memo
/// keys, written only under ObjectCache's lock and never held across Python
/// code), and each memo's Python dict, whose single operations free-threaded
/// CPython runs atomically — no interior mutability the GIL was papering over. PyO3
/// enforces `Send + Sync` on every pyclass at compile time.
#[cfg(feature = "python")]
#[pymodule(gil_used = false)]
fn _rust_serializer(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Add byte storage class
    m.add_class::<python_bindings::PyByteStorage>()?;

    // Per-function L1-hit statistics, counted without a lock (decorators/wrapper.py)
    m.add_class::<hit_counter::PyHitCounter>()?;

    // The memoised L1-only hit, its entries and their clock (decorators/wrapper.py, object_cache.py)
    m.add_class::<l1_hit::HitMemo>()?;
    m.add_class::<l1_hit::HitEntry>()?;
    m.add_function(wrap_pyfunction!(l1_hit::memo_key_of, m)?)?;
    m.add_function(wrap_pyfunction!(l1_hit::now, m)?)?;
    m.add_function(wrap_pyfunction!(l1_hit::set_clock, m)?)?;
    m.add("MEMO_MAX_LEN", l1_hit::MEMO_MAX_LEN)?;
    m.add("MEMO_MAX_ARGS", l1_hit::MEMO_MAX_ARGS)?;
    m.add("MEMO_MAX_ENTRIES", l1_hit::MEMO_MAX_ENTRIES)?;

    // ByteStorage.retrieve() envelope-verification-failure taxonomy. Same
    // __module__ patch as KeyringConfigurationError below: create_exception! sets it to the
    // bare "_rust_serializer", which breaks pickling back to a parent process otherwise.
    let envelope_integrity_error = m.py().get_type::<python_bindings::EnvelopeIntegrityError>();
    envelope_integrity_error.setattr("__module__", "cachekit._rust_serializer")?;
    m.add("EnvelopeIntegrityError", envelope_integrity_error)?;

    // Standalone integrity primitive — registered unconditionally (usable with
    // the checksum feature alone; must not vanish when encryption is off)
    m.add_function(wrap_pyfunction!(python_bindings::checksum_py, m)?)?;
    m.add_function(wrap_pyfunction!(python_bindings::verify_checksum_py, m)?)?;

    // Untrusted-decode structural bound — zero-copy header walk that
    // serializers/base.py::unpackb_bounded runs before every msgpack.unpackb
    m.add_function(wrap_pyfunction!(
        python_bindings::check_msgpack_structure_py,
        m
    )?)?;
    // Test-only probe of the zero-copy buffer borrow (tests/critical/test_decrypt_buffer_input.py)
    m.add_function(wrap_pyfunction!(python_bindings::bytes_view_borrows_py, m)?)?;

    // Add encryption functionality if feature is enabled
    #[cfg(feature = "encryption")]
    {
        python_bindings::register_encryption_module(m)?;
    }

    // Module metadata
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("__author__", "cachekit team")?;

    #[cfg(feature = "encryption")]
    m.add(
        "__description__",
        "Raw byte storage with LZ4 compression, xxHash3-64 checksums, and zero-knowledge encryption",
    )?;

    #[cfg(not(feature = "encryption"))]
    m.add(
        "__description__",
        "Raw byte storage layer with LZ4 compression and xxHash3-64 checksums",
    )?;

    Ok(())
}
