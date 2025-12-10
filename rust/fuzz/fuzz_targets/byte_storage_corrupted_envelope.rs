#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::StorageEnvelope;

fuzz_target!(|data: &[u8]| {
    // Attack: Attempt to deserialize arbitrary bytes as StorageEnvelope
    // Validates: Safe rejection of corrupted MessagePack serialization without panics

    // Try to deserialize fuzz input as MessagePack StorageEnvelope
    match rmp_serde::from_slice::<StorageEnvelope>(data) {
        Ok(envelope) => {
            // Successfully deserialized - now test extract() validation
            // extract() should either succeed (valid envelope) or reject safely (invalid)
            match envelope.extract() {
                Ok(_decompressed) => {
                    // Valid envelope and decompression succeeded - this is acceptable
                    // (rare but possible if fuzzer generates valid structure)
                }
                Err(_) => {
                    // Envelope validation or decompression failed - expected for most malformed input
                    // This is the fail-closed behavior we want
                }
            }
        }
        Err(_) => {
            // Deserialization failed - expected for invalid MessagePack
            // This is also safe fail-closed behavior
        }
    }

    // Success: No panics occurred, all paths handled safely
    // Invariant: Malformed MessagePack must be rejected without crashes
});
