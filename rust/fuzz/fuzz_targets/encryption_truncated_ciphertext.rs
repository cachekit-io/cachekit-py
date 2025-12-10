#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;

fuzz_target!(|data: &[u8]| {
    // Attack: Truncated ciphertext (incomplete nonce, auth tag)
    // Validates: Authentication failure detection on truncated input

    // Limit data size for performance
    if data.len() > 1024 || data.len() < 32 {
        return;
    }

    let (key, plaintext) = data.split_at(32);
    let aad = b"test_aad";

    let encryptor = ZeroKnowledgeEncryptor::new();

    // Create valid ciphertext
    let ciphertext = match encryptor.encrypt_aes_gcm(plaintext, key, aad) {
        Ok(ct) => ct,
        Err(_) => return, // Invalid key length
    };

    // Verify valid ciphertext decrypts successfully
    assert!(
        encryptor.decrypt_aes_gcm(&ciphertext, key, aad).is_ok(),
        "Valid ciphertext should decrypt"
    );

    // AES-GCM-256 format: 12-byte nonce + ciphertext + 16-byte auth tag
    // Minimum valid length: 12 + 0 + 16 = 28 bytes

    // Test truncation at various points
    let truncation_points = [
        0,                              // Empty
        1,                              // Single byte
        11,                             // Incomplete nonce
        12,                             // Nonce only
        ciphertext.len() / 2,          // Halfway
        ciphertext.len().saturating_sub(16), // Missing auth tag
        ciphertext.len().saturating_sub(1),  // Missing 1 byte
    ];

    for &trunc_len in &truncation_points {
        if trunc_len >= ciphertext.len() {
            continue; // Skip if truncation would be no-op
        }

        let truncated = &ciphertext[..trunc_len];

        // Truncated ciphertext MUST fail authentication
        match encryptor.decrypt_aes_gcm(truncated, key, aad) {
            Ok(_) => {
                panic!(
                    "Truncated ciphertext (len={}) should fail authentication, but succeeded",
                    trunc_len
                );
            }
            Err(_) => {
                // Expected: Authentication should fail
                // Error should indicate invalid format or authentication failure
            }
        }
    }

    // Test empty ciphertext specifically
    match encryptor.decrypt_aes_gcm(&[], key, aad) {
        Ok(_) => panic!("Empty ciphertext should fail"),
        Err(_) => {
            // Expected
        }
    }

    // Success: All truncated ciphertexts rejected
    // Invariant: Incomplete auth tags must cause authentication failure
});
