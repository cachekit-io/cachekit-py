#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;
use std::collections::HashSet;

fuzz_target!(|data: &[u8]| {
    // Attack: Nonce reuse detection (encrypt same plaintext 100x)
    // Validates: Cryptographic nonce uniqueness (no ciphertext repetition)

    // Limit plaintext size for performance
    if data.len() > 4096 {
        return;
    }

    // Split data into key and plaintext
    if data.len() < 32 {
        return; // Need at least 32 bytes for key
    }

    let (key, plaintext) = data.split_at(32);
    let aad = b"fuzz_test_aad";

    let encryptor = ZeroKnowledgeEncryptor::new();

    // Encrypt same plaintext multiple times
    let mut ciphertexts = HashSet::new();
    let iterations = if plaintext.len() < 100 { 100 } else { 20 }; // Fewer iterations for larger data

    for _ in 0..iterations {
        match encryptor.encrypt_aes_gcm(plaintext, key, aad) {
            Ok(ciphertext) => {
                // Verify ciphertext is unique (nonce uniqueness)
                let is_new = ciphertexts.insert(ciphertext.clone());

                if !is_new {
                    panic!(
                        "Nonce reuse detected: same ciphertext produced twice for same plaintext"
                    );
                }
            }
            Err(_) => {
                // Encryption failure is acceptable for invalid key length
                return;
            }
        }
    }

    // Success: All ciphertexts are unique (probabilistic nonce uniqueness verified)
    // Invariant: Nonces must be unique (no ciphertext repetition)
    assert!(
        ciphertexts.len() == iterations,
        "Expected {} unique ciphertexts, got {}",
        iterations,
        ciphertexts.len()
    );
});
