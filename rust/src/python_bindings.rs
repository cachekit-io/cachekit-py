//! Python bindings for cachekit-core
//!
//! This module provides thin PyO3 wrappers around cachekit-core functionality.
//! All business logic is delegated to cachekit-core.

use cachekit_core::ByteStorage;
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Python wrapper for ByteStorage
#[pyclass(name = "ByteStorage")]
pub struct PyByteStorage {
    inner: ByteStorage,
}

impl Default for PyByteStorage {
    fn default() -> Self {
        Self::new(None)
    }
}

#[pymethods]
impl PyByteStorage {
    #[new]
    pub fn new(default_format: Option<String>) -> Self {
        Self {
            inner: ByteStorage::new(default_format),
        }
    }

    /// Store arbitrary bytes with compression and checksums
    ///
    /// Args:
    ///     data: Raw bytes to store
    ///     format: Optional format identifier (defaults to "msgpack")
    ///
    /// Returns:
    ///     Bytes: Serialized StorageEnvelope
    pub fn store(&self, py: Python, data: &[u8], format: Option<String>) -> PyResult<Py<PyBytes>> {
        // Detach from the GIL: LZ4 + xxh3 on a large payload otherwise blocks every
        // Python thread for the full compression duration (cachekit-core#45).
        // Sound: `data` borrows an immutable `bytes` buffer kept alive by this call.
        let envelope_bytes = py
            .detach(|| self.inner.store(data, format))
            .map_err(|e| PyValueError::new_err(format!("Storage failed: {}", e)))?;

        Ok(PyBytes::new(py, &envelope_bytes).into())
    }

    /// Retrieve and validate stored bytes
    ///
    /// Args:
    ///     envelope_bytes: Serialized StorageEnvelope bytes
    ///
    /// Returns:
    ///     Tuple[bytes, str]: (original_data, format_identifier)
    pub fn retrieve(&self, py: Python, envelope_bytes: &[u8]) -> PyResult<(Vec<u8>, String)> {
        // Detach from the GIL for decompression + checksum (see store()).
        py.detach(|| self.inner.retrieve(envelope_bytes))
            .map_err(|e| PyValueError::new_err(format!("Retrieval failed: {}", e)))
    }

    /// Get compression ratio for given data
    pub fn estimate_compression(&self, py: Python, data: &[u8]) -> PyResult<f64> {
        // Full-payload LZ4 pass — same GIL-blocking profile as store().
        py.detach(|| self.inner.estimate_compression(data))
            .map_err(|e| PyValueError::new_err(format!("Compression estimation failed: {}", e)))
    }

    /// Validate envelope without extracting data
    pub fn validate(&self, py: Python, envelope_bytes: &[u8]) -> PyResult<bool> {
        // Full decompression + checksum under the hood — same GIL-blocking profile.
        Ok(py.detach(|| self.inner.validate(envelope_bytes)))
    }

    /// Get security limits for clients
    #[getter]
    pub fn max_uncompressed_size(&self) -> PyResult<usize> {
        Ok(self.inner.max_uncompressed_size())
    }

    #[getter]
    pub fn max_compressed_size(&self) -> PyResult<usize> {
        Ok(self.inner.max_compressed_size())
    }

    #[getter]
    pub fn max_compression_ratio(&self) -> PyResult<f64> {
        Ok(self.inner.max_compression_ratio() as f64)
    }
}

// ========== Encryption Bindings (feature-gated) ==========

#[cfg(feature = "encryption")]
use cachekit_core::{
    encryption::{
        key_derivation::{derive_domain_key, derive_tenant_keys, key_fingerprint, TenantKeys},
        key_rotation::KeyRotationState,
    },
    ZeroKnowledgeEncryptor,
};

/// Python wrapper for ZeroKnowledgeEncryptor
#[cfg(feature = "encryption")]
#[pyclass(name = "ZeroKnowledgeEncryptor")]
pub struct PyZeroKnowledgeEncryptor {
    inner: ZeroKnowledgeEncryptor,
}

// Note: Default is not implemented because ZeroKnowledgeEncryptor::new() is fallible

#[cfg(feature = "encryption")]
#[pymethods]
impl PyZeroKnowledgeEncryptor {
    #[new]
    pub fn new() -> PyResult<Self> {
        let inner = ZeroKnowledgeEncryptor::new()
            .map_err(|e| PyValueError::new_err(format!("Failed to create encryptor: {}", e)))?;
        Ok(Self { inner })
    }

    /// Encrypt data using AES-256-GCM
    #[pyo3(name = "encrypt")]
    pub fn encrypt_py(&self, plaintext: &[u8], key: &[u8], aad: &[u8]) -> PyResult<Vec<u8>> {
        self.inner
            .encrypt_aes_gcm(plaintext, key, aad)
            .map_err(|e| PyValueError::new_err(format!("Encryption failed: {}", e)))
    }

    /// Decrypt data using AES-256-GCM
    #[pyo3(name = "decrypt")]
    pub fn decrypt_py(&self, ciphertext: &[u8], key: &[u8], aad: &[u8]) -> PyResult<Vec<u8>> {
        self.inner
            .decrypt_aes_gcm(ciphertext, key, aad)
            .map_err(|e| PyValueError::new_err(format!("Decryption failed: {}", e)))
    }

    /// Encrypt data with keys that never leave Rust memory
    #[pyo3(name = "encrypt_with_keys")]
    pub fn encrypt_with_keys(
        &self,
        plaintext: &[u8],
        aad: &[u8],
        tenant_keys: &PyTenantKeys,
    ) -> PyResult<Vec<u8>> {
        let encryption_key = &tenant_keys.inner.encryption_key;

        self.inner
            .encrypt_aes_gcm(plaintext, encryption_key, aad)
            .map_err(|e| PyValueError::new_err(format!("Encryption failed: {}", e)))
    }

    /// Decrypt data with keys that never leave Rust memory
    #[pyo3(name = "decrypt_with_keys")]
    pub fn decrypt_with_keys(
        &self,
        ciphertext: &[u8],
        aad: &[u8],
        tenant_keys: &PyTenantKeys,
    ) -> PyResult<Vec<u8>> {
        let encryption_key = &tenant_keys.inner.encryption_key;

        self.inner
            .decrypt_aes_gcm(ciphertext, encryption_key, aad)
            .map_err(|e| PyValueError::new_err(format!("Decryption failed: {}", e)))
    }

    /// Check if hardware acceleration is enabled
    #[pyo3(name = "hardware_acceleration_enabled")]
    pub fn hardware_acceleration_enabled(&self) -> bool {
        self.inner.hardware_acceleration_enabled()
    }

    /// Get current nonce counter value for monitoring
    #[pyo3(name = "get_nonce_counter")]
    pub fn get_nonce_counter(&self) -> u64 {
        self.inner.get_nonce_counter()
    }

    /// Get metrics from last encryption/decryption operation
    #[pyo3(name = "get_last_metrics")]
    pub fn get_last_metrics(&self) -> PyResult<Py<PyOperationMetrics>> {
        let metrics = self.inner.get_last_metrics();
        let py_metrics = PyOperationMetrics {
            compression_time_micros: metrics.compression_time_micros,
            compression_ratio: metrics.compression_ratio,
            checksum_time_micros: metrics.checksum_time_micros,
            encryption_time_micros: metrics.encryption_time_micros,
            hardware_accelerated: metrics.hardware_accelerated,
        };
        // pyo3 0.29 renamed Python::with_gil -> Python::attach (GIL/free-threaded terminology).
        Python::attach(|py| Py::new(py, py_metrics))
    }
}

/// Python wrapper for TenantKeys
///
/// Note: Clone is intentionally not derived - key material should never be
/// duplicated in memory. Always pass by reference to minimize exposure.
#[cfg(feature = "encryption")]
#[pyclass(name = "TenantKeys")]
pub struct PyTenantKeys {
    pub(crate) inner: TenantKeys,
}

#[cfg(feature = "encryption")]
#[pymethods]
impl PyTenantKeys {
    #[getter]
    pub fn tenant_id(&self) -> String {
        self.inner.tenant_id.clone()
    }

    #[pyo3(name = "encryption_fingerprint")]
    pub fn encryption_fingerprint(&self) -> Vec<u8> {
        self.inner.encryption_fingerprint().to_vec()
    }

    #[pyo3(name = "authentication_fingerprint")]
    pub fn authentication_fingerprint(&self) -> Vec<u8> {
        self.inner.authentication_fingerprint().to_vec()
    }
}

/// Python wrapper for OperationMetrics
#[pyclass(name = "OperationMetrics")]
pub struct PyOperationMetrics {
    #[pyo3(get)]
    pub compression_time_micros: u64,
    #[pyo3(get)]
    pub compression_ratio: f64,
    #[pyo3(get)]
    pub checksum_time_micros: u64,
    #[pyo3(get)]
    pub encryption_time_micros: Option<u64>,
    #[pyo3(get)]
    pub hardware_accelerated: bool,
}

#[pymethods]
impl PyOperationMetrics {
    pub fn __repr__(&self) -> String {
        format!(
            "OperationMetrics(compression_time={}, ratio={:.2}, encryption_time={:?}, hw_accel={})",
            self.compression_time_micros,
            self.compression_ratio,
            self.encryption_time_micros,
            self.hardware_accelerated
        )
    }
}

/// Python wrapper for KeyRotationState
#[cfg(feature = "encryption")]
#[pyclass(name = "KeyRotationState")]
pub struct PyKeyRotationState {
    inner: KeyRotationState,
}

#[cfg(feature = "encryption")]
#[pymethods]
impl PyKeyRotationState {
    #[new]
    pub fn new(key: &[u8]) -> PyResult<Self> {
        if key.len() != 32 {
            return Err(PyValueError::new_err(format!(
                "Key must be 32 bytes, got {}",
                key.len()
            )));
        }
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(key);
        Ok(Self {
            inner: KeyRotationState::new(key_array),
        })
    }

    /// Start key rotation with new key
    #[pyo3(name = "start_rotation")]
    pub fn start_rotation(&mut self, new_key: &[u8]) -> PyResult<()> {
        if new_key.len() != 32 {
            return Err(PyValueError::new_err(format!(
                "Key must be 32 bytes, got {}",
                new_key.len()
            )));
        }
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(new_key);
        self.inner.start_rotation(key_array);
        Ok(())
    }

    /// Complete key rotation (remove old key)
    #[pyo3(name = "complete_rotation")]
    pub fn complete_rotation(&mut self) {
        self.inner.complete_rotation();
    }

    /// Check if rotation is currently in progress
    #[pyo3(name = "is_rotating")]
    pub fn is_rotating(&self) -> bool {
        self.inner.is_rotating()
    }
}

// Note: Error conversions are done inline with .map_err() to avoid orphan rule violations

// ========== Python function exports ==========

/// Derive a domain-specific key using HKDF-SHA256
#[cfg(feature = "encryption")]
#[pyfunction]
#[pyo3(name = "derive_domain_key")]
pub fn derive_domain_key_py(
    master_key: &[u8],
    domain: &str,
    tenant_salt: &[u8],
) -> PyResult<Vec<u8>> {
    let key = derive_domain_key(master_key, domain, tenant_salt)
        .map_err(|e| PyValueError::new_err(format!("Key derivation failed: {}", e)))?;

    Ok(key.to_vec())
}

/// Derive all tenant keys at once
#[cfg(feature = "encryption")]
#[pyfunction]
#[pyo3(name = "derive_tenant_keys")]
pub fn derive_tenant_keys_py(master_key: &[u8], tenant_id: &str) -> PyResult<PyTenantKeys> {
    let keys = derive_tenant_keys(master_key, tenant_id)
        .map_err(|e| PyValueError::new_err(format!("Tenant key derivation failed: {}", e)))?;

    Ok(PyTenantKeys { inner: keys })
}

/// Generate key fingerprint
#[cfg(feature = "encryption")]
#[pyfunction]
#[pyo3(name = "key_fingerprint")]
pub fn key_fingerprint_py(key: &[u8]) -> Vec<u8> {
    key_fingerprint(key).to_vec()
}

/// Compute the standalone xxHash3-64 checksum of `data` (8 bytes, big-endian).
///
/// Accepts any buffer-protocol object — `bytes`, `bytearray`, `memoryview`,
/// Arrow buffers — so a serializer holding its payload as a `memoryview`
/// (e.g. Arrow IPC) can hash it directly, without forcing a `bytes` copy.
///
/// NON-cryptographic: detects corruption, not tampering. For tamper-resistance
/// use @cache.secure (AES-256-GCM), never this checksum. Produces the exact
/// bytes embedded in every StorageEnvelope, without the LZ4 compression
/// overhead — for serializers where compression is ineffective (Arrow IPC, JSON).
#[pyfunction]
#[pyo3(name = "checksum")]
pub fn checksum_py(py: Python, data: PyBuffer<u8>) -> PyResult<Py<PyBytes>> {
    let data = data.to_vec(py)?;
    Ok(PyBytes::new(py, &cachekit_core::checksum(&data)).into())
}

/// Verify `data` against an expected 8-byte xxHash3-64 checksum.
///
/// Both arguments accept any buffer-protocol object (`bytes`, `bytearray`,
/// `memoryview`, …) — the Arrow verify path slices a `memoryview` (`mv[8:]`),
/// so a bytes-only signature would break the moment a serializer moves onto
/// this FFI.
///
/// NON-cryptographic: detects corruption, not tampering (see `checksum`).
/// Raises ValueError if `expected` is not exactly 8 bytes — a truncated
/// checksum must fail loudly, never return a wrong verdict.
#[pyfunction]
#[pyo3(name = "verify_checksum")]
pub fn verify_checksum_py(
    py: Python,
    data: PyBuffer<u8>,
    expected: PyBuffer<u8>,
) -> PyResult<bool> {
    let expected: [u8; 8] = expected.to_vec(py)?.try_into().map_err(|v: Vec<u8>| {
        PyValueError::new_err(format!("expected must be exactly 8 bytes, got {}", v.len()))
    })?;
    let data = data.to_vec(py)?;
    Ok(cachekit_core::verify_checksum(&data, &expected))
}

/// Register encryption module with Python
#[cfg(feature = "encryption")]
pub fn register_encryption_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyZeroKnowledgeEncryptor>()?;
    m.add_class::<PyTenantKeys>()?;
    m.add_class::<PyOperationMetrics>()?;
    m.add_class::<PyKeyRotationState>()?;
    m.add_function(wrap_pyfunction!(derive_domain_key_py, m)?)?;
    m.add_function(wrap_pyfunction!(derive_tenant_keys_py, m)?)?;
    m.add_function(wrap_pyfunction!(key_fingerprint_py, m)?)?;

    Ok(())
}
