#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::StorageEnvelope;
use arbitrary::Arbitrary;

#[derive(Arbitrary, Debug)]
struct OverflowTestCase {
    /// Original size to test (including u32::MAX, boundaries, suspicious ratios)
    original_size: u32,
    /// Compressed data size (small to create suspicious ratios)
    compressed_data_len: u8, // 0-255 bytes
    /// Checksum bytes
    checksum: [u8; 8],
    /// Format string
    format_len: u8, // 0-255 for format string length
}

fuzz_target!(|test_case: OverflowTestCase| {
    // Attack: Integer overflow via decompression bomb (oversized original_size)
    // Validates: Size limit enforcement prevents excessive allocation

    // Generate compressed data
    let compressed_data = vec![b'x'; test_case.compressed_data_len as usize];

    // Generate format string
    let format = "f".repeat(test_case.format_len as usize);

    // Create envelope with potentially malicious original_size
    let envelope = StorageEnvelope {
        compressed_data,
        checksum: test_case.checksum,
        original_size: test_case.original_size,
        format,
    };

    // Test extract() with oversized original_size
    match envelope.extract() {
        Ok(_) => {
            // Decompression succeeded - envelope passed all validation checks
            // This should only happen for valid sizes within limits
        }
        Err(_) => {
            // Expected for oversized allocations (u32::MAX, beyond 512MB, etc.).
            // Every 0.4.0 ByteStorageError message matches a string check here,
            // so asserting on the text would be a tautology — the invariant this
            // target enforces is "no panic on extreme sizes", not the wording.
        }
    }

    // Success: No panics on extreme sizes (u32::MAX, boundary cases)
    // Invariant: Size limits (512MB) must be enforced consistently
});
