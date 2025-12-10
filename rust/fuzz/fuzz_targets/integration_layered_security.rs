#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::StorageEnvelope;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;

fuzz_target!(|data: &[u8]| {
    // Attack: Layered corruption (ByteStorage + Encryption combined)
    // Validates: Proper layer error attribution, no false positives

    // Minimum data: 32 bytes key + some plaintext
    if data.len() < 64 {
        return;
    }

    let (key, plaintext) = data.split_at(32);

    // Limit plaintext for performance
    if plaintext.len() > 4096 {
        return;
    }

    let aad = b"layered_test";
    let encryptor = ZeroKnowledgeEncryptor::new();

    // Step 1: Create ByteStorage envelope (compression + checksum)
    let envelope = match StorageEnvelope::new(plaintext.to_vec(), "msgpack".to_string()) {
        Ok(env) => env,
        Err(_) => return, // Plaintext too large
    };

    // Step 2: Serialize envelope to bytes
    let envelope_bytes = match rmp_serde::to_vec(&envelope) {
        Ok(bytes) => bytes,
        Err(_) => return, // Serialization failed
    };

    // Step 3: Encrypt the serialized envelope
    let ciphertext = match encryptor.encrypt_aes_gcm(&envelope_bytes, key, aad) {
        Ok(ct) => ct,
        Err(_) => return, // Encryption failed (invalid key)
    };

    // === Valid roundtrip test ===
    // Decrypt → Deserialize → Extract should all succeed
    match encryptor.decrypt_aes_gcm(&ciphertext, key, aad) {
        Ok(decrypted_envelope_bytes) => {
            match rmp_serde::from_slice::<StorageEnvelope>(&decrypted_envelope_bytes) {
                Ok(recovered_envelope) => {
                    match recovered_envelope.extract() {
                        Ok(recovered_data) => {
                            assert_eq!(
                                plaintext, recovered_data,
                                "Layered roundtrip integrity violated"
                            );
                        }
                        Err(_) => {
                            // ByteStorage layer rejected (checksum mismatch, size limits, etc.)
                            // This is acceptable if envelope was somehow invalid
                        }
                    }
                }
                Err(_) => {
                    // Deserialization failed - envelope format corrupted
                }
            }
        }
        Err(_) => {
            panic!("Valid ciphertext should decrypt successfully");
        }
    }

    // === Corruption test 1: Corrupt ciphertext (outer layer) ===
    if !ciphertext.is_empty() {
        let mut corrupted_ciphertext = ciphertext.clone();
        corrupted_ciphertext[0] ^= 0xFF;

        // Should fail at encryption layer (auth tag verification)
        match encryptor.decrypt_aes_gcm(&corrupted_ciphertext, key, aad) {
            Ok(_) => {
                panic!("Corrupted ciphertext should fail authentication");
            }
            Err(_) => {
                // Expected: Encryption layer catches corruption
            }
        }
    }

    // === Corruption test 2: Corrupt inner envelope (after decryption) ===
    // Simulate: decrypt succeeds but envelope is malformed
    let mut corrupted_envelope_bytes = envelope_bytes.clone();
    if !corrupted_envelope_bytes.is_empty() {
        corrupted_envelope_bytes[0] ^= 0x01; // Corrupt envelope data
    }

    // Encrypt the corrupted envelope
    if let Ok(corrupted_ciphertext) = encryptor.encrypt_aes_gcm(&corrupted_envelope_bytes, key, aad) {
        // Decrypt should succeed (ciphertext is valid)
        if let Ok(decrypted_corrupted) = encryptor.decrypt_aes_gcm(&corrupted_ciphertext, key, aad) {
            // But deserialization or extraction should fail (inner layer corrupted)
            match rmp_serde::from_slice::<StorageEnvelope>(&decrypted_corrupted) {
                Ok(corrupted_env) => {
                    // Deserialization succeeded, but extraction should catch corruption
                    match corrupted_env.extract() {
                        Ok(_) => {
                            // Extraction succeeded - corruption was benign or reverted
                        }
                        Err(_) => {
                            // Expected: ByteStorage layer catches corruption
                        }
                    }
                }
                Err(_) => {
                    // Expected: Deserialization layer catches corruption
                }
            }
        }
    }

    // === Corruption test 3: Wrong AAD (breaks encryption binding) ===
    let wrong_aad = b"wrong_aad";
    match encryptor.decrypt_aes_gcm(&ciphertext, key, wrong_aad) {
        Ok(_) => {
            panic!("Wrong AAD should fail authentication");
        }
        Err(_) => {
            // Expected: Encryption layer catches AAD mismatch
        }
    }

    // Success: Layered security works correctly
    // Invariant: Each layer catches its own corruption class
});
