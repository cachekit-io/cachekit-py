#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::ByteStorage;

fuzz_target!(|data: &[u8]| {
    // Create ByteStorage instance
    let storage = ByteStorage::new(Some("fuzz".to_string()));

    // Test compression (should never panic)
    if let Ok(envelope_bytes) = storage.store(data, None) {
        // Verify roundtrip property: compress → decompress == original
        if let Ok((decompressed, format)) = storage.retrieve(&envelope_bytes) {
            // Roundtrip property must hold
            assert_eq!(data, decompressed.as_slice(), "Roundtrip property violated");
            assert_eq!("fuzz", format, "Format mismatch");
        }
    }

    // Test estimate_compression (should never panic on valid sizes)
    let _ = storage.estimate_compression(data);
});
