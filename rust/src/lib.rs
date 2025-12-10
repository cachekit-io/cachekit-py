//! `PyO3` bindings for `cachekit-core`
//!
//! This crate provides thin Python wrappers around the cachekit-core library.
//! All business logic lives in cachekit-core; this crate only handles Python FFI.

// Re-export core types for use in Python bindings
pub use cachekit_core::{ByteStorage, OperationMetrics, StorageEnvelope};

#[cfg(feature = "encryption")]
pub use cachekit_core::{
    EncryptionError, ZeroKnowledgeEncryptor, derive_domain_key,
    encryption::{
        key_derivation::{TenantKeys, derive_tenant_keys, key_fingerprint},
        key_rotation::KeyRotationState,
    },
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
        "Raw byte storage with LZ4 compression, Blake3 checksums, and zero-knowledge encryption",
    )?;

    #[cfg(not(feature = "encryption"))]
    m.add(
        "__description__",
        "Raw byte storage layer with LZ4 compression and Blake3 checksums",
    )?;

    Ok(())
}
