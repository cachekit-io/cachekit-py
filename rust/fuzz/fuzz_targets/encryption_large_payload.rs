#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;

fuzz_target!(|data: &[u8]| {
    // Attack: Production-scale payloads (no artificial limits like existing 4KB)
    // Validates: Encryption correctness and performance at scale

    // Accept arbitrary input up to 100MB (libfuzzer will explore various sizes)
    // No artificial 4KB limit (removed from existing encryption_roundtrip.rs)
    if data.len() > 100 * 1024 * 1024 {
        return; // 100MB hard limit for CI timeout prevention
    }

    if data.len() < 32 {
        return; // Need at least 32 bytes for key
    }

    let (key, plaintext) = data.split_at(32);
    let aad = b"large_payload_test";

    let encryptor = ZeroKnowledgeEncryptor::new();

    // Test encryption → decryption roundtrip
    let start = std::time::Instant::now();

    let ciphertext = match encryptor.encrypt_aes_gcm(plaintext, key, aad) {
        Ok(ct) => ct,
        Err(_) => return, // Invalid key length
    };

    let elapsed_encrypt = start.elapsed();

    // Verify reasonable performance (prevent CI timeout)
    // 100MB should encrypt in < 5 seconds even on slow CI
    if plaintext.len() > 10 * 1024 * 1024 && elapsed_encrypt.as_secs() > 5 {
        panic!(
            "Encryption too slow: {}MB took {:?}",
            plaintext.len() / 1024 / 1024,
            elapsed_encrypt
        );
    }

    // Test decryption
    let start_decrypt = std::time::Instant::now();
    match encryptor.decrypt_aes_gcm(&ciphertext, key, aad) {
        Ok(decrypted) => {
            let elapsed_decrypt = start_decrypt.elapsed();

            // Verify roundtrip correctness
            assert_eq!(
                plaintext,
                decrypted.as_slice(),
                "Roundtrip failed at {} bytes",
                plaintext.len()
            );

            // Verify reasonable decrypt performance
            if plaintext.len() > 10 * 1024 * 1024 && elapsed_decrypt.as_secs() > 5 {
                panic!(
                    "Decryption too slow: {}MB took {:?}",
                    plaintext.len() / 1024 / 1024,
                    elapsed_decrypt
                );
            }
        }
        Err(_) => {
            panic!("Decryption failed after successful encryption (roundtrip violated)");
        }
    }

    // Test specific large sizes explicitly if input is small
    if data.len() < 1024 {
        let large_sizes = [1024 * 1024, 10 * 1024 * 1024]; // 1MB, 10MB

        for size in large_sizes {
            let large_plaintext = vec![b'x'; size];

            if let Ok(ct) = encryptor.encrypt_aes_gcm(&large_plaintext, key, aad) {
                if let Ok(decrypted) = encryptor.decrypt_aes_gcm(&ct, key, aad) {
                    assert_eq!(
                        large_plaintext.len(),
                        decrypted.len(),
                        "Large payload size mismatch at {}MB",
                        size / 1024 / 1024
                    );
                }
            }
        }
    }

    // Success: Large payloads encrypt/decrypt correctly with reasonable performance
    // Invariant: No artificial size limits, correctness at production scale
});
