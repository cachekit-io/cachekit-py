//! `PyO3` bindings for `cachekit-core`
//!
//! This crate provides thin Python wrappers around the cachekit-core library.
//! Business logic lives in cachekit-core, including the untrusted msgpack decode bound
//! (`cachekit_core::check_msgpack_structure`).

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

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Python module definition - exports raw byte storage and encryption
///
/// `gil_used = false` (the PyO3 0.28+ default, made explicit): declares the
/// module thread-safe under free-threaded CPython so importing it does not
/// force the GIL back on. Verified by audit: every `#[pyclass]`
/// exposes only `&self` methods, and shared state in cachekit-core is atomic
/// or Mutex-guarded — no interior mutability the GIL was papering over. PyO3
/// enforces `Send + Sync` on every pyclass at compile time.
#[cfg(feature = "python")]
#[pymodule(gil_used = false)]
fn _rust_serializer(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Add byte storage class
    m.add_class::<python_bindings::PyByteStorage>()?;

    // Per-function L1-hit statistics, counted without a lock (decorators/wrapper.py)
    m.add_class::<hit_counter::PyHitCounter>()?;

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
