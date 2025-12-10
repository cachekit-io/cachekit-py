#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::StorageEnvelope;
use arbitrary::Arbitrary;

#[derive(Arbitrary, Debug)]
struct EmptyDataTestCase {
    /// Original size (can be non-zero)
    original_size: u32,
    /// Compressed data length (can be 0)
    compressed_len: u8, // 0-255
    /// Checksum
    checksum: [u8; 32],
}

fuzz_target!(|test_case: EmptyDataTestCase| {
    // Attack: Inconsistent envelope state (empty data + non-zero size, etc.)
    // Validates: Envelope consistency checks reject mismatched states

    let compressed_data = vec![b'x'; test_case.compressed_len as usize];

    let envelope = StorageEnvelope {
        compressed_data,
        checksum: test_case.checksum,
        original_size: test_case.original_size,
        format: "msgpack".to_string(),
    };

    // Test extract() with potentially inconsistent state
    match envelope.extract() {
        Ok(decompressed) => {
            // If successful, verify consistency
            // Decompressed length should match original_size (within reason)
            assert_eq!(
                decompressed.len(),
                test_case.original_size as usize,
                "Decompressed length must match original_size"
            );
        }
        Err(_) => {
            // Expected for inconsistent states:
            // - Empty compressed_data with non-zero original_size
            // - original_size=0 but compressed_data non-empty (suspicious)
            // - Decompression fails due to invalid LZ4 data
            // - Checksum mismatch
            // All safe rejections
        }
    }

    // Specific edge case: Empty compressed_data with non-zero original_size
    if test_case.compressed_len == 0 && test_case.original_size > 0 {
        let empty_envelope = StorageEnvelope {
            compressed_data: vec![],
            checksum: test_case.checksum,
            original_size: test_case.original_size,
            format: "msgpack".to_string(),
        };

        // This MUST fail (cannot decompress empty data to non-zero size)
        match empty_envelope.extract() {
            Ok(_) => {
                panic!("Empty compressed_data with non-zero original_size should fail");
            }
            Err(_) => {
                // Expected: LZ4 decompression should fail on empty input
            }
        }
    }

    // Specific edge case: Both empty (should handle gracefully)
    if test_case.compressed_len == 0 && test_case.original_size == 0 {
        let both_empty = StorageEnvelope {
            compressed_data: vec![],
            checksum: test_case.checksum,
            original_size: 0,
            format: "msgpack".to_string(),
        };

        // May succeed or fail depending on LZ4 behavior with empty input
        let _ = both_empty.extract();
        // Just ensure no panic - behavior is implementation-defined
    }

    // Success: Inconsistent envelope states are rejected safely
    // Invariant: Envelope consistency must be validated
});
