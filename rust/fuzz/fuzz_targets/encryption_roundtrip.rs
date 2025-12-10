#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;

fuzz_target!(|data: &[u8]| {
    // Limit data size for reasonable fuzzing time (encryption is expensive)
    if data.len() > 4096 {
        return;
    }

    // Create encryptor
    let encryptor = ZeroKnowledgeEncryptor::new();

    // Generate deterministic key and AAD from input
    // (In real usage, keys come from key derivation)
    let key = if data.len() >= 32 {
        &data[0..32]
    } else {
        // Pad with zeros if input too short
        let mut padded = [0u8; 32];
        padded[..data.len()].copy_from_slice(data);
        return; // Skip fuzzing with padded keys - focus on real data
    };

    let aad = b"fuzz_test_aad";
    let plaintext = if data.len() > 32 {
        &data[32..]
    } else {
        b"test_plaintext"
    };

    // Test encryption (should not panic on any input)
    match encryptor.encrypt_aes_gcm(plaintext, key, aad) {
        Ok(ciphertext) => {
            // Test roundtrip property: encrypt → decrypt == original
            match encryptor.decrypt_aes_gcm(&ciphertext, key, aad) {
                Ok(decrypted) => {
                    assert_eq!(
                        plaintext, decrypted.as_slice(),
                        "Encryption roundtrip property violated"
                    );
                }
                Err(_) => {
                    panic!("Decryption failed after successful encryption (roundtrip violated)");
                }
            }

            // Test tamper detection: modify ciphertext and verify decryption fails
            if !ciphertext.is_empty() {
                let mut tampered = ciphertext.clone();
                tampered[0] ^= 0xFF; // Flip bits in first byte

                match encryptor.decrypt_aes_gcm(&tampered, key, aad) {
                    Ok(_) => {
                        panic!("Tamper detection failed: modified ciphertext decrypted successfully");
                    }
                    Err(_) => {
                        // Expected: AES-GCM authentication should catch tampering
                    }
                }
            }

            // Test AAD binding: decrypt with wrong AAD should fail
            let wrong_aad = b"wrong_aad";
            match encryptor.decrypt_aes_gcm(&ciphertext, key, wrong_aad) {
                Ok(_) => {
                    panic!("AAD binding failed: decryption succeeded with wrong AAD");
                }
                Err(_) => {
                    // Expected: AAD mismatch should fail authentication
                }
            }
        }
        Err(_) => {
            // Encryption failure is acceptable for malformed inputs
            // (e.g., invalid key length, though we enforce 32 bytes above)
        }
    }
});
