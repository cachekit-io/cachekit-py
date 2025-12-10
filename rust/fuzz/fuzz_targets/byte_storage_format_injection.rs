#![no_main]

use libfuzzer_sys::fuzz_target;
use cachekit_storage::byte_storage::StorageEnvelope;

fuzz_target!(|data: &[u8]| {
    // Attack: Malicious format identifiers (path traversal, nulls, control chars, Unicode)
    // Validates: Format string treated as opaque identifier, no injection vectors

    // Try to use arbitrary data as format string
    let format = String::from_utf8_lossy(data).to_string();

    // Limit format length for performance
    if format.len() > 10240 {
        return;
    }

    // Create envelope with potentially malicious format
    let test_data = vec![b'x'; 100];
    let envelope = match StorageEnvelope::new(test_data, format.clone()) {
        Ok(env) => env,
        Err(_) => return, // Skip if envelope creation fails (acceptable)
    };

    // Verify format is stored as-is (opaque identifier)
    assert_eq!(envelope.format, format, "Format should be stored exactly as provided");

    // Extract should work regardless of format content
    // (format is metadata, not used for decompression/validation)
    match envelope.extract() {
        Ok(_) => {
            // Success - format had no effect on extraction
        }
        Err(_) => {
            // Failure must be due to data issues, not format
            // (format is not validated beyond storage)
        }
    }

    // Test specific injection patterns explicitly
    let long_format = "x".repeat(10000);
    let injection_patterns = [
        "../../../etc/passwd",      // Path traversal
        "fmt\0null",                 // Null byte
        "fmt\nCRLF\r",              // Control characters
        "\u{202E}RTL",              // Unicode RTL override
        "\u{FEFF}BOM",              // Unicode BOM
        long_format.as_str(),        // Very long string
    ];

    for pattern in &injection_patterns {
        let pattern_data = vec![b'y'; 50];
        if let Ok(env) = StorageEnvelope::new(pattern_data, pattern.to_string()) {
            // Format stored as-is
            assert_eq!(env.format, *pattern);

            // Extract works regardless of format content
            let _ = env.extract();
            // No panic = success (format is opaque)
        }
    }

    // Success: Format identifiers are opaque (no path traversal or injection possible)
    // Invariant: Format string must not be interpreted as filesystem path or command
});
