#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::encryption::core::ZeroKnowledgeEncryptor;

fuzz_target!(|data: &[u8]| {
    // Attack: AAD with nulls, control characters, Unicode
    // Validates: AAD cryptographic binding (wrong AAD = decrypt fails)

    // Limit size for performance
    if data.len() > 2048 || data.len() < 32 {
        return;
    }

    let (key, rest) = data.split_at(32);
    if rest.len() < 100 {
        return;
    }

    let (plaintext, aad) = rest.split_at(rest.len() / 2);

    let encryptor = ZeroKnowledgeEncryptor::new();

    // Encrypt with potentially malicious AAD
    // AAD can contain: nulls, control chars, Unicode, empty, long strings
    let ciphertext = match encryptor.encrypt_aes_gcm(plaintext, key, aad) {
        Ok(ct) => ct,
        Err(_) => return, // Invalid key
    };

    // Decrypt with correct AAD should succeed
    match encryptor.decrypt_aes_gcm(&ciphertext, key, aad) {
        Ok(decrypted) => {
            assert_eq!(plaintext, decrypted.as_slice(), "Roundtrip with correct AAD");
        }
        Err(_) => {
            panic!("Decryption with correct AAD should succeed");
        }
    }

    // Decrypt with wrong AAD should fail (AAD binding verification)
    let wrong_aad = b"wrong_aad_value";
    match encryptor.decrypt_aes_gcm(&ciphertext, key, wrong_aad) {
        Ok(_) => {
            panic!("Decryption with wrong AAD should fail (AAD binding violated)");
        }
        Err(_) => {
            // Expected: AAD mismatch causes authentication failure
        }
    }

    // Test 1-bit flip in AAD (should cause authentication failure)
    if !aad.is_empty() {
        let mut modified_aad = aad.to_vec();
        modified_aad[0] ^= 0x01; // Flip one bit

        match encryptor.decrypt_aes_gcm(&ciphertext, key, &modified_aad) {
            Ok(_) => {
                panic!("1-bit AAD modification should fail authentication");
            }
            Err(_) => {
                // Expected
            }
        }
    }

    // Test specific malicious AAD patterns
    let long_aad = vec![b'a'; 10000];
    let malicious_aad_patterns: Vec<&[u8]> = vec![
        b"",                              // Empty AAD
        b"aad\x00null",                   // Null byte
        b"aad\n\r\t",                     // Control characters
        b"\xEF\xBB\xBFBOM",              // UTF-8 BOM
        &long_aad,                        // Very long AAD (10KB)
    ];

    for pattern_aad in malicious_aad_patterns {
        // Encrypt with pattern AAD
        if let Ok(ct) = encryptor.encrypt_aes_gcm(plaintext, key, pattern_aad) {
            // Decrypt with same AAD should succeed
            assert!(
                encryptor.decrypt_aes_gcm(&ct, key, pattern_aad).is_ok(),
                "Correct AAD should always work"
            );

            // Decrypt with different AAD should fail
            let different_aad = b"different";
            assert!(
                encryptor.decrypt_aes_gcm(&ct, key, different_aad).is_err(),
                "Wrong AAD should fail"
            );
        }
    }

    // Success: AAD binding is cryptographically enforced
    // Invariant: Wrong AAD must cause authentication failure
});
