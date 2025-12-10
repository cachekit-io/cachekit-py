#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::ByteStorage;

fuzz_target!(|data: &[u8]| {
    // Create ByteStorage instance
    let storage = ByteStorage::new(Some("fuzz".to_string()));

    // Test decompression with malicious/malformed inputs
    // CRITICAL: Must NEVER panic, only return Err
    let result = storage.retrieve(data);

    // Security properties to verify:
    match result {
        Ok((decompressed, _format)) => {
            // If decompression succeeded, validate size limits
            assert!(
                decompressed.len() <= storage.max_uncompressed_size(),
                "Decompression bomb: exceeded size limit"
            );
        }
        Err(_) => {
            // Expected behavior for malformed/malicious input
            // Error handling is correct - no panic occurred
        }
    }

    // Test validation (should never panic)
    let is_valid = storage.validate(data);

    // If validation says it's valid, retrieve must succeed
    if is_valid {
        let retrieve_result = storage.retrieve(data);
        assert!(
            retrieve_result.is_ok(),
            "Validation returned true but retrieve failed"
        );
    }
});
