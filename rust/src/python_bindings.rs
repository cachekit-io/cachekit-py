//! Python bindings for cachekit-core
//!
//! This module provides thin PyO3 wrappers around cachekit-core functionality, plus the
//! buffer-borrow helper they share. Business logic lives in cachekit-core.

use cachekit_core::byte_storage::ByteStorageError;
use cachekit_core::{check_msgpack_structure, ByteStorage};
use pyo3::buffer::PyBuffer;
#[cfg(feature = "encryption")]
use pyo3::exceptions::PyBufferError;
use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

pyo3::create_exception!(
    _rust_serializer,
    EnvelopeIntegrityError,
    PyValueError,
    "A ByteStorage envelope parsed but failed verification: checksum mismatch, decompression\n\
     bomb/failure, or a decoded size mismatch against the envelope header.\n\
     \n\
     Distinguishes a verified-but-corrupt envelope (this exception) from bytes that were never\n\
     a ByteStorage envelope at all (`DeserializationFailed`, e.g. written with integrity\n\
     checking off) — the latter stays a plain `ValueError`. Whether a caller may fall through\n\
     on that `ValueError` is the caller's contract (`AutoSerializer.deserialize` does so only\n\
     for a metadata-less direct call); this exception must always fail closed.\n\
     \n\
     Subclasses ValueError so existing `pytest.raises(ValueError)` assertions on `retrieve()`\n\
     failures stay valid. `AutoSerializer.deserialize` catches this specifically and re-raises\n\
     it as `SerializationError` without falling through."
);

/// Map a cachekit-core `retrieve()` failure onto the Python exception taxonomy.
///
/// `DeserializationFailed` means `envelope_bytes` never parsed as a `StorageEnvelope` — not
/// corruption, just "not an envelope" — so it stays a plain `ValueError`, the fall-through
/// signal `AutoSerializer.deserialize` depends on. Every other variant is mapped to
/// `EnvelopeIntegrityError` and must fail closed: the post-parse checks (checksum, decompressed
/// size, decompression itself, the compression-ratio bomb guard) are genuine corruption or
/// tampering, and `InputTooLarge` — raised on the raw `envelope_bytes` length before parsing is
/// even attempted — is deliberately bucketed the same way rather than treated as "not an
/// envelope": falling through would hand an oversized blob to the plain-msgpack decode path
/// instead of rejecting it outright, trading one size guard for a weaker one.
fn retrieve_error_to_py(err: ByteStorageError) -> PyErr {
    let message = format!("Retrieval failed: {}", err);
    match err {
        ByteStorageError::DeserializationFailed(_) => PyValueError::new_err(message),
        _ => EnvelopeIntegrityError::new_err(message),
    }
}

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

/// Offset into `base` at which `buf`'s memory starts, iff `buf` is a plain, immutable,
/// C-contiguous window onto that `bytes` object — the shape `SerializationWrapper.unwrap`
/// produces. `Some(off)` means `&base.as_bytes()[off..off + buf.item_count()]` is exactly
/// the buffer's bytes, so the caller can borrow it with a safe slice instead of a raw one.
///
/// Every conjunct is load-bearing: `readonly` + a `bytes` base rule out a mutable exporter
/// racing the GIL-released read; `is_c_contiguous` rules out a strided view whose logical
/// bytes are not the contiguous span; the range check rules out a spoofed `.obj` naming a
/// decoy `bytes` the memory does not belong to; and `checked_add` keeps that range check
/// honest against a forged `Py_buffer` length rather than wrapping past it.
fn borrowable_offset(buf: &PyBuffer<u8>, base: &Bound<'_, PyBytes>) -> Option<usize> {
    let bytes = base.as_bytes();
    let start = bytes.as_ptr() as usize;
    let ptr = buf.buf_ptr() as usize;
    let end = ptr.checked_add(buf.item_count())?;
    (buf.readonly()
        && buf.is_c_contiguous()
        && buf.item_count() > 0
        && ptr >= start
        && end <= start + bytes.len())
    .then(|| ptr - start)
}

/// A read-only view of a Python buffer-protocol object's bytes, borrowed without a copy
/// whenever that is provably sound and copied otherwise. Holds whatever keeps the memory
/// alive (the `bytes` object, or the owned copy) so `as_slice` needs no `unsafe`.
enum BytesView<'py> {
    /// `(base, offset, len)`: a window onto an immutable `bytes` object kept alive by the
    /// Bound — the whole object, or the read-only C-contiguous `memoryview` of it that
    /// `SerializationWrapper.unwrap` produces, proven by `borrowable_offset`. Zero-copy.
    Borrowed(Bound<'py, PyBytes>, usize, usize),
    /// Mutable, non-`bytes`-backed, strided, or empty exporter: the only safe answer is a copy.
    Owned(Vec<u8>),
}

impl BytesView<'_> {
    fn as_slice(&self) -> &[u8] {
        match self {
            BytesView::Borrowed(base, off, len) => &base.as_bytes()[*off..*off + *len],
            BytesView::Owned(v) => v,
        }
    }
}

/// Borrow `obj`'s bytes zero-copy when the BACKING STORAGE is provably immutable, else copy.
///
/// `readonly()` describes the view, not the exporter (`memoryview(bytearray).toreadonly()`
/// passes it while another thread can still mutate the bytearray), and a PEP 688
/// `__buffer__` exporter can name a decoy `bytes` in `.obj` — so the gate is the containment
/// proof in `borrowable_offset`, whose payoff is that the borrow is an ORDINARY SLICE of that
/// `bytes`: bounds-checked by Rust, no `unsafe`, nothing for a stale comment to misstate.
fn bytes_view<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<BytesView<'py>> {
    if let Ok(b) = obj.cast::<PyBytes>() {
        // The real length, never `len()`: that calls a subclass's `__len__`, which can lie and
        // make `as_slice` panic (too long) or truncate the input (too short).
        let len = b.as_bytes().len();
        return Ok(BytesView::Borrowed(b.clone(), 0, len));
    }
    let buf = PyBuffer::<u8>::get(obj)?;
    // Interned: a plain "obj" would build a new str on every call, which costs a small
    // decrypt more than the bytes() copy this borrow replaces.
    let base = obj
        .getattr(intern!(py, "obj"))
        .ok()
        .and_then(|base| base.cast_into::<PyBytes>().ok());
    if let Some(base) = base {
        if let Some(off) = borrowable_offset(&buf, &base) {
            return Ok(BytesView::Borrowed(base, off, buf.item_count()));
        }
    }
    Ok(BytesView::Owned(buf.to_vec(py)?))
}

/// Reject a MessagePack document that nests deeper than `max_depth` (every array or map
/// header counts, an empty one included) or whose headers would make decoding it allocate
/// out of proportion to its size — cachekit-core's `check_msgpack_structure`, run over a
/// zero-copy view for `bytes` and for read-only `memoryview`s of `bytes`. Raises ValueError
/// naming the violated bound.
#[pyfunction]
#[pyo3(name = "check_msgpack_structure")]
pub fn check_msgpack_structure_py(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    max_depth: usize,
) -> PyResult<()> {
    let view = bytes_view(py, data)?;
    check_msgpack_structure(view.as_slice(), max_depth).map_err(|what| {
        PyValueError::new_err(format!("Unpack failed: MessagePack document {what}"))
    })
}

/// Whether `bytes_view` borrows `obj` (True) or copies it (False). Test-only: it pins what
/// `bytes_view` decides for an input, which tracemalloc cannot see (a Rust-side copy never
/// touches the Python heap). It does not pin how a caller uses that view: that `retrieve` and
/// the decrypt bindings pass `as_slice()` on without copying it is held by review of their code.
#[pyfunction]
#[pyo3(name = "_bytes_view_borrows")]
pub fn bytes_view_borrows_py(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(matches!(bytes_view(py, obj)?, BytesView::Borrowed(..)))
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
    ///     envelope_bytes: Serialized StorageEnvelope — any buffer-protocol object
    ///         (`bytes`, `memoryview`, `bytearray`), so callers holding a zero-copy
    ///         `memoryview` (SerializationWrapper.unwrap) never re-coerce to `bytes`
    ///
    /// Returns:
    ///     Tuple[bytes, str]: (original_data, format_identifier)
    pub fn retrieve(
        &self,
        py: Python,
        envelope_bytes: &Bound<'_, PyAny>,
    ) -> PyResult<(Vec<u8>, String)> {
        // Borrowing across the GIL release below is only sound when the backing storage
        // is immutable — bytes_view proves that or copies (see its doc).
        let view = bytes_view(py, envelope_bytes)?;
        let data = view.as_slice();
        // Detach from the GIL for decompression + checksum (see store()).
        py.detach(|| self.inner.retrieve(data))
            .map_err(retrieve_error_to_py)
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
    encryption::key_derivation::{
        derive_domain_key, derive_tenant_keys, key_fingerprint, TenantKeys,
    },
    EncryptionError, Keyring, TenantKeyring, ZeroKnowledgeEncryptor,
};
#[cfg(feature = "encryption")]
use zeroize::Zeroizing;

/// The decrypt bindings' ciphertext: `bytes_view`, except that a buffer whose format is not
/// `B` or `c` (a signed-byte `memoryview.cast('b')`, say) is read as its raw bytes, exactly as
/// the `bytes(data)` coercion these bindings replaced read it. Without this, such a view of an
/// authentic frame would fail before authentication and surface as tampering.
#[cfg(feature = "encryption")]
fn ciphertext_view<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<BytesView<'py>> {
    match bytes_view(py, obj) {
        Err(err) if err.is_instance_of::<PyBufferError>(py) => {
            // bytes(obj) is a fresh, immutable copy, so borrowing it is sound.
            let copy = py
                .get_type::<PyBytes>()
                .call1((obj,))?
                .cast_into::<PyBytes>()?;
            let len = copy.as_bytes().len();
            Ok(BytesView::Borrowed(copy, 0, len))
        }
        view => view,
    }
}

#[cfg(feature = "encryption")]
pyo3::create_exception!(
    _rust_serializer,
    KeyringConfigurationError,
    PyValueError,
    "A LOCAL keyring configuration fault.\n\
     \n\
     Strictly limited to faults whose input is our own configuration or build.\n\
     On the decrypt path: a keyring entry index that does not exist\n\
     (`KeyringIndexOutOfRange`). At EncryptionWrapper construction: a rejected\n\
     keyring (`Keyring(...)`: cap exceeded, current key in the decrypt-only\n\
     list), a previous master key below the 32-byte floor, a failure deriving\n\
     the keyring's per-tenant keys or a second binding (`Keyring.for_tenant`),\n\
     and the\n\
     keyring-entry-0 vs derive_tenant_keys fingerprint drift guard. These are\n\
     deploy, caller or build bugs: recording them as `auth_tamper` pages an\n\
     operator for an attack that never happened, and letting them become a miss\n\
     hides a broken keyring behind silent recomputes.\n\
     \n\
     Everything whose input is the STORED CIPHERTEXT stays on the tamper path,\n\
     including short/garbled ciphertext. An attacker with backend write access can\n\
     truncate an entry, and `decrypt_aes_gcm` rejects it on length BEFORE the tag\n\
     check — so classifying structural errors as config would let the attacker\n\
     choose whether the tamper alarm fires. This mirrors cachekit-rs, which maps\n\
     only KeyDerivation | KeyringIndexOutOfRange to its Config class.\n\
     \n\
     Subclasses ValueError. Note that cachekit-py's read path routes on\n\
     SerializationError, so callers that must not fail open re-raise this\n\
     explicitly (see cache_handler.py)."
);

/// Map a cachekit-core decrypt-path error onto the Python exception taxonomy.
///
/// The split is by INPUT PROVENANCE, not by "is it AuthenticationFailed":
/// attacker-supplied ciphertext faults must stay tamper-class, our own config
/// faults must not. Mirrors the cachekit-rs mapping exactly so the three SDKs
/// tell operators the same story.
#[cfg(feature = "encryption")]
fn decrypt_error_to_py(err: EncryptionError) -> PyErr {
    match err {
        // KeyDerivation is unreachable from TenantKeyring (it derives at construction); the arm
        // keeps the mapping identical to cachekit-rs's.
        EncryptionError::KeyDerivation(_) | EncryptionError::KeyringIndexOutOfRange { .. } => {
            KeyringConfigurationError::new_err(format!("Keyring decrypt failed: {}", err))
        }
        other => PyValueError::new_err(format!("Decryption failed: {}", other)),
    }
}

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
    ///
    /// `ciphertext` is any buffer-protocol object (`bytes`, `bytearray`, `memoryview`, ...), read
    /// as its raw bytes the way `bytes(ciphertext)` reads it. Immutable `bytes`, or a read-only
    /// view of it such as `SerializationWrapper.unwrap` returns, is read without a copy; any
    /// other buffer is copied first.
    #[pyo3(name = "decrypt_with_keys")]
    pub fn decrypt_with_keys(
        &self,
        py: Python<'_>,
        ciphertext: &Bound<'_, PyAny>,
        aad: &[u8],
        tenant_keys: &PyTenantKeys,
    ) -> PyResult<Vec<u8>> {
        let encryption_key = &tenant_keys.inner.encryption_key;
        let ciphertext = ciphertext_view(py, ciphertext)?;

        self.inner
            .decrypt_aes_gcm(ciphertext.as_slice(), encryption_key, aad)
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

/// Python wrapper for the master-key rotation Keyring (spec/encryption.md →
/// "Key Rotation (Keyring)").
///
/// Master-key material enters once at construction (config ingestion) and
/// never leaves. The only thing this class does after construction is bind
/// one tenant: [`for_tenant`](Self::for_tenant) derives that tenant's keys and
/// hands back a [`PyTenantKeyring`], which decrypts with no HKDF. All keyring
/// key material zeroizes on drop inside cachekit-core, decrypt-only entries
/// included; binding drops the master keys at once.
#[cfg(feature = "encryption")]
#[pyclass(name = "Keyring")]
pub struct PyKeyring {
    /// `None` once [`for_tenant`](Self::for_tenant) has consumed it.
    inner: Option<Keyring>,
}

#[cfg(feature = "encryption")]
#[pymethods]
impl PyKeyring {
    /// Build a keyring from the current master key plus decrypt-only previous
    /// keys. cachekit-core validates the cap (max 3 decrypt-only keys, never
    /// truncated), rejects the current key re-appearing in the decrypt-only
    /// list (detectable subset of the forward-only invariant), and enforces
    /// minimum key length.
    ///
    /// Note the two different length floors: cachekit-core accepts any key of
    /// **at least 16 bytes**, while cachekit-py requires **32 bytes** for both
    /// the current and every previous key (enforced Python-side in
    /// `encryption_wrapper.py`). The stricter Python floor is deliberate and is
    /// the one operators are held to; the core minimum is stated here only so
    /// the FFI contract is not mistaken for the product contract.
    #[new]
    pub fn new(current: &[u8], decrypt_only: Vec<Vec<u8>>) -> PyResult<Self> {
        // `decrypt_only` is a fresh PyO3-side allocation of real key material.
        // `Keyring::new` copies what it needs (and zeroizes its own copies on
        // drop), so without this wrapper these vectors would be freed with the
        // previous master keys still in the heap pages.
        let decrypt_only: Vec<Zeroizing<Vec<u8>>> =
            decrypt_only.into_iter().map(Zeroizing::new).collect();
        let refs: Vec<&[u8]> = decrypt_only.iter().map(|key| key.as_slice()).collect();
        let inner = Keyring::new(current, &refs).map_err(|e| {
            KeyringConfigurationError::new_err(format!("Keyring configuration invalid: {}", e))
        })?;
        Ok(Self { inner: Some(inner) })
    }

    /// Bind the keyring to `tenant_id`: derive each entry's per-tenant
    /// encryption key once (HKDF, current key first) and return the bound ring.
    ///
    /// Consumes this keyring, as core's `Keyring::for_tenant` does: the master
    /// keys are zeroized here, success or failure, and a second call raises
    /// `KeyringConfigurationError`. A derivation failure is
    /// `KeyringConfigurationError` too: the wrapper binds only a tenant that
    /// `derive_tenant_keys` has already accepted, so what fails here is our
    /// own build, never the input.
    #[pyo3(name = "for_tenant")]
    pub fn for_tenant(&mut self, tenant_id: &str) -> PyResult<PyTenantKeyring> {
        let keyring = self.inner.take().ok_or_else(|| {
            KeyringConfigurationError::new_err("Keyring is already bound to a tenant")
        })?;
        let inner = keyring.for_tenant(tenant_id).map_err(|e| {
            KeyringConfigurationError::new_err(format!(
                "Keyring tenant key derivation failed: {}",
                e
            ))
        })?;
        Ok(PyTenantKeyring { inner })
    }
}

/// Python wrapper for a keyring bound to one tenant: the derived per-tenant
/// encryption key of each keyring entry, current key first, and no master-key
/// material. Nothing on it runs HKDF; the keys were derived by
/// `Keyring.for_tenant`. No accessor hands key bytes back to Python.
#[cfg(feature = "encryption")]
#[pyclass(name = "TenantKeyring", frozen)]
pub struct PyTenantKeyring {
    inner: TenantKeyring,
}

#[cfg(feature = "encryption")]
#[pymethods]
impl PyTenantKeyring {
    /// Per-entry fingerprints of the derived per-tenant **encryption** key, in
    /// attempt order (current key first). This is the value cachekit-py
    /// stores as CK frame metadata, so fingerprint-based keyring selection
    /// compares like with like. Entry count is `len()` of this list.
    #[pyo3(name = "encryption_fingerprints")]
    pub fn encryption_fingerprints(&self) -> Vec<Vec<u8>> {
        self.inner
            .encryption_fingerprints()
            .into_iter()
            .map(|fp| fp.to_vec())
            .collect()
    }

    /// Decrypt with the keyring entry at `index` (0 = current key).
    ///
    /// For fingerprint-based selection: a fingerprint match is binding — if
    /// the matched entry fails AES-GCM authentication the failure is terminal,
    /// and the caller must not retry other keyring entries. This method never
    /// falls back across entries.
    ///
    /// An out-of-range `index` is a caller bug (`KeyringConfigurationError`),
    /// unreachable when `index` comes from a match against this ring's own
    /// `encryption_fingerprints`.
    ///
    /// `ciphertext` takes the same buffers as `decrypt_with_keys`, read the same way.
    #[pyo3(name = "decrypt_at")]
    pub fn decrypt_at(
        &self,
        py: Python<'_>,
        index: usize,
        encryptor: &PyZeroKnowledgeEncryptor,
        ciphertext: &Bound<'_, PyAny>,
        aad: &[u8],
    ) -> PyResult<Vec<u8>> {
        let ciphertext = ciphertext_view(py, ciphertext)?;
        self.inner
            .decrypt_at(index, &encryptor.inner, ciphertext.as_slice(), aad)
            .map_err(decrypt_error_to_py)
    }

    /// Decrypt by sequential keyring attempts: current key first, then each
    /// decrypt-only key in order, with the identical `aad` for every attempt.
    ///
    /// For entries WITHOUT per-entry key identity (interop mode — no CK frame,
    /// so no stored key fingerprint), per the spec's "Decrypt — without
    /// per-entry key identity" row. Only an AES-GCM authentication failure
    /// advances to the next key; structural errors are terminal. Exhaustion
    /// surfaces as a plain authentication failure — the caller's existing
    /// fail-open/fail-closed policy applies, no new failure mode. Entries WITH
    /// a stored fingerprint must use fingerprint selection (`decrypt_at`),
    /// never this method.
    ///
    /// `ciphertext` takes the same buffers as `decrypt_with_keys`, read the same way.
    #[pyo3(name = "decrypt")]
    pub fn decrypt(
        &self,
        py: Python<'_>,
        encryptor: &PyZeroKnowledgeEncryptor,
        ciphertext: &Bound<'_, PyAny>,
        aad: &[u8],
    ) -> PyResult<Vec<u8>> {
        let ciphertext = ciphertext_view(py, ciphertext)?;
        self.inner
            .decrypt(&encryptor.inner, ciphertext.as_slice(), aad)
            .map_err(decrypt_error_to_py)
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
    m.add_class::<PyKeyring>()?;
    m.add_class::<PyTenantKeyring>()?;
    let keyring_config_error = m.py().get_type::<KeyringConfigurationError>();
    // create_exception! sets __module__ to the bare "_rust_serializer"; without
    // this the class cannot be pickled back to a parent process, so a
    // ProcessPoolExecutor worker surfaces ModuleNotFoundError instead of the
    // real failure.
    keyring_config_error.setattr("__module__", "cachekit._rust_serializer")?;
    m.add("KeyringConfigurationError", keyring_config_error)?;
    m.add_function(wrap_pyfunction!(derive_domain_key_py, m)?)?;
    m.add_function(wrap_pyfunction!(derive_tenant_keys_py, m)?)?;
    m.add_function(wrap_pyfunction!(key_fingerprint_py, m)?)?;

    Ok(())
}
