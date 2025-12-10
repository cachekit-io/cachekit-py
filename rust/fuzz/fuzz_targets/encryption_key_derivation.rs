#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::key_derivation::derive_domain_key;

fuzz_target!(|data: &[u8]| {
    // Attack: Malicious tenant IDs in key derivation (nulls, path traversal, Unicode)
    // Validates: Deterministic key derivation without crashes

    // Split input into master_key and tenant_salt
    if data.len() < 16 {
        return; // Need at least 16 bytes for valid master key
    }

    let (master_key, tenant_salt) = data.split_at(16);

    // Test key derivation with malicious tenant salt
    // tenant_salt can contain: nulls, path traversal, Unicode, empty, long strings
    match derive_domain_key(master_key, "encryption", tenant_salt) {
        Ok(derived_key) => {
            // Key derivation succeeded - verify output properties
            assert_eq!(derived_key.len(), 32, "Derived key must be 32 bytes");

            // Test determinism: same inputs → same output
            let derived_key2 = derive_domain_key(master_key, "encryption", tenant_salt)
                .expect("Deterministic derivation should succeed again");
            assert_eq!(
                derived_key, derived_key2,
                "Key derivation must be deterministic"
            );
        }
        Err(_) => {
            // Key derivation failed - acceptable if master_key too short
            // (already validated above, but may fail on additional checks)
        }
    }

    // Test specific malicious patterns
    let long_tenant = vec![b'x'; 10000];
    let malicious_patterns: Vec<&[u8]> = vec![
        b"../../../admin",          // Path traversal
        b"tenant\x00null",           // Null byte
        b"\xEF\xBB\xBFBOM_tenant",  // UTF-8 BOM
        b"",                         // Empty tenant
        &long_tenant,                // Very long tenant ID (10KB)
        b"tenant\nCRLF\r",          // Control characters
    ];

    for pattern in malicious_patterns {
        if master_key.len() >= 16 {
            match derive_domain_key(master_key, "encryption", pattern) {
                Ok(key) => {
                    assert_eq!(key.len(), 32);
                    // Verify same pattern produces same key (determinism)
                    let key2 = derive_domain_key(master_key, "encryption", pattern)
                        .expect("Should be deterministic");
                    assert_eq!(key, key2, "Determinism violated for pattern");
                }
                Err(_) => {
                    // Acceptable failure
                }
            }
        }
    }

    // Success: Key derivation handles malicious tenant IDs safely
    // Invariant: Deterministic output (same input → same key), no panics
});
