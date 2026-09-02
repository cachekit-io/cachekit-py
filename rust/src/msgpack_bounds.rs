//! Structural bound for untrusted MessagePack (LAB-2503; protocol spec/interop-mode.md →
//! Decode bounds). The one algorithm this crate owns rather than delegates to cachekit-core;
//! a core-shared walk usable from py/rs/wasm is the follow-up. Mirrors the opcode table of
//! cachekit-rs `check_structure` so the two SDKs reject the same documents.

/// Header-only walk over one MessagePack document: str/bin/ext payloads are skipped by
/// offset, never read, and nothing is allocated beyond one `u64` per open collection.
///
/// Rejects, before any decoder pre-allocates a container:
/// - nesting deeper than `max_depth`;
/// - a header declaring more payload bytes than the input holds;
/// - more pending elements (across every open collection) than remaining bytes can back —
///   every element costs >= 1 byte, so a decoder's total container pre-allocation is then
///   bounded by the input length instead of by `depth × declared_len`;
/// - the reserved marker 0xc1 and input that ends mid-document.
///
/// Trailing bytes after the root element are left to the decoder (`ExtraData`).
pub fn check_msgpack_structure(bytes: &[u8], max_depth: usize) -> Result<(), String> {
    fn be(bytes: &[u8], pos: usize, width: usize) -> Result<u64, String> {
        let end = pos
            .checked_add(width)
            .filter(|e| *e <= bytes.len())
            .ok_or_else(|| "ends inside a length prefix".to_owned())?;
        Ok(bytes[pos..end]
            .iter()
            .fold(0u64, |acc, b| (acc << 8) | u64::from(*b)))
    }

    let mut pos = 0usize;
    let mut pending: u64 = 1; // elements owed across all open collections (the root is one)
    let mut open: Vec<u64> = Vec::new(); // elements still owed per open collection = depth
    while pending > 0 {
        while open.last() == Some(&0) {
            open.pop();
        }
        let marker = *bytes
            .get(pos)
            .ok_or_else(|| "ends before the document is complete".to_owned())?;
        pos += 1;
        pending -= 1;
        if let Some(innermost) = open.last_mut() {
            *innermost -= 1;
        }
        // (length-prefix bytes, payload bytes after the prefix, child elements)
        let (prefix, payload, children): (usize, u64, u64) = match marker {
            0x00..=0x7f | 0xc0 | 0xc2 | 0xc3 | 0xe0..=0xff => (0, 0, 0),
            0x80..=0x8f => (0, 0, 2 * u64::from(marker & 0x0f)),
            0x90..=0x9f => (0, 0, u64::from(marker & 0x0f)),
            0xa0..=0xbf => (0, u64::from(marker & 0x1f), 0),
            0xc1 => return Err("contains the reserved marker 0xc1".to_owned()),
            0xc4 | 0xd9 => (1, be(bytes, pos, 1)?, 0),
            0xc5 | 0xda => (2, be(bytes, pos, 2)?, 0),
            0xc6 | 0xdb => (4, be(bytes, pos, 4)?, 0),
            0xc7 => (1, be(bytes, pos, 1)? + 1, 0), // ext: length prefix, then type byte + data
            0xc8 => (2, be(bytes, pos, 2)? + 1, 0),
            0xc9 => (4, be(bytes, pos, 4)? + 1, 0),
            0xca..=0xd3 => (0, 1u64 << (marker & 0x03), 0), // f32/f64/u8..u64/i8..i64: 4,8,1,2,4,8,1,2,4,8
            0xd4..=0xd8 => (0, 1 + (1u64 << (marker - 0xd4)), 0), // fixext: type byte + 1/2/4/8/16
            0xdc => (2, 0, be(bytes, pos, 2)?),
            0xdd => (4, 0, be(bytes, pos, 4)?),
            0xde => (2, 0, 2 * be(bytes, pos, 2)?),
            0xdf => (4, 0, 2 * be(bytes, pos, 4)?),
        };
        pos += prefix;
        let remaining = (bytes.len() - pos) as u64;
        if payload > remaining {
            return Err("declares more bytes than the input holds".to_owned());
        }
        pos += payload as usize; // <= remaining, so it fits usize
        if children > 0 {
            if open.len() >= max_depth {
                return Err(format!("nests deeper than {max_depth} levels"));
            }
            open.push(children);
        }
        pending += children;
        if pending > remaining - payload {
            return Err("declares more elements than the input can back".to_owned());
        }
    }
    Ok(())
}
