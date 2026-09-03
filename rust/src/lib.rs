//! `PyO3` bindings for `cachekit-core`
//!
//! This crate provides thin Python wrappers around the cachekit-core library.
//! Business logic lives in cachekit-core, with one SDK-owned exception: the untrusted
//! msgpack decode bound in `msgpack_bounds` (LAB-2503), pending a core-shared walk.

// Re-export core types for use in Python bindings
pub use cachekit_core::{ByteStorage, OperationMetrics, StorageEnvelope};

/// Untrusted msgpack structural bound — pure Rust, not gated on `python`
pub mod msgpack_bounds;

#[cfg(feature = "encryption")]
pub use cachekit_core::{
    derive_domain_key,
    encryption::key_derivation::{derive_tenant_keys, key_fingerprint, TenantKeys},
    EncryptionError, Keyring, ZeroKnowledgeEncryptor,
};

// Python bindings (gated behind python feature)
#[cfg(feature = "python")]
pub mod python_bindings;

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Python module definition - exports raw byte storage and encryption
#[cfg(feature = "python")]
#[pymodule]
fn _rust_serializer(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Add byte storage class
    m.add_class::<python_bindings::PyByteStorage>()?;

    // Standalone integrity primitive — registered unconditionally (usable with
    // the checksum feature alone; must not vanish when encryption is off)
    m.add_function(wrap_pyfunction!(python_bindings::checksum_py, m)?)?;
    m.add_function(wrap_pyfunction!(python_bindings::verify_checksum_py, m)?)?;

    // Untrusted-decode structural bound (LAB-2503) — zero-copy header walk that
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
