#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::{ByteStorageError, StorageEnvelope};
use arbitrary::Arbitrary;

#[derive(Arbitrary, Debug)]
struct ChecksumTestCase {
    /// Data to compress (will be used for valid envelope)
    data: Vec<u8>,
    /// Bit flip position in compressed_data, reduced mod payload length.
    /// u32, not u8: payloads run to 4096 bytes, so a u8 index would confine
    /// every flip this target ever generates to the first 256 bytes.
    flip_byte_idx: u32,
    /// Bit flip mask
    flip_mask: u8,
}

fuzz_target!(|test_case: ChecksumTestCase| {
    // Attack: Checksum collision via data corruption
    // Validates: the xxHash3-64 integrity check (8-byte, NON-cryptographic —
    // it detects corruption, it does not resist a chosen-collision attacker)
    // rejects a mutated payload, or else returns the original bytes unchanged.

    // Limit data size for fuzzing performance
    if test_case.data.len() > 4096 {
        return;
    }

    // Create valid envelope first
    let envelope = match StorageEnvelope::new(&test_case.data, "msgpack".to_string()) {
        Ok(env) => env,
        Err(_) => return, // Skip if data too large
    };

    // Valid envelope should extract successfully
    let valid_result = envelope.extract();
    assert!(
        valid_result.is_ok(),
        "Valid envelope should extract successfully"
    );

    // Now corrupt the compressed data (simulate bit flip/corruption)
    let mut corrupted_envelope = StorageEnvelope {
        compressed_data: envelope.compressed_data.clone(),
        checksum: envelope.checksum,
        original_size: envelope.original_size,
        format: envelope.format.clone(),
    };

    if !corrupted_envelope.compressed_data.is_empty() {
        let idx = (test_case.flip_byte_idx as usize) % corrupted_envelope.compressed_data.len();
        corrupted_envelope.compressed_data[idx] ^= test_case.flip_mask;
    }

    // The invariant is reject-or-return-original. Asserting only on the Err arm
    // would let the one bug this target exists to find — corrupted bytes
    // extracting *successfully* as different data — pass silently.
    match corrupted_envelope.extract() {
        Ok(recovered) => assert_eq!(
            recovered, test_case.data,
            "corrupted envelope extracted successfully but returned different data \
             — integrity check bypassed"
        ),
        Err(err) => assert!(
            matches!(
                err,
                ByteStorageError::ChecksumMismatch | ByteStorageError::DecompressionFailed
            ),
            // Match the variant, not err.to_string(): asserting on Display text
            // makes a #[error(...)] reword in a core release fail this gate on a
            // false crash.
            "corruption must surface as ChecksumMismatch or DecompressionFailed, got: {err:?}"
        ),
    }

    // Test with completely wrong checksum
    let wrong_checksum_envelope = StorageEnvelope {
        compressed_data: envelope.compressed_data.clone(),
        checksum: [0xFF; 8], // Wrong checksum
        original_size: envelope.original_size,
        format: envelope.format.clone(),
    };

    // Should be rejected unless the genuine checksum happened to be all 0xFF —
    // which is the only case the Ok arm may accept, so assert exactly that.
    match wrong_checksum_envelope.extract() {
        Ok(_) => assert_eq!(
            envelope.checksum, [0xFF; 8],
            "forged all-0xFF checksum accepted over a payload whose real checksum \
             was {:?} — integrity check bypassed",
            envelope.checksum
        ),
        Err(err) => assert!(
            matches!(
                err,
                ByteStorageError::ChecksumMismatch | ByteStorageError::DecompressionFailed
            ),
            "forged checksum must surface as ChecksumMismatch or DecompressionFailed, got: {err:?}"
        ),
    }

    // Invariant: the xxHash3-64 integrity check must catch data tampering — a
    // mutated payload either fails to extract or yields the original bytes.
});
