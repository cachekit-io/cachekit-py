//! pymemcache's rendezvous node choice, natively: the Memcached backend's `HashClient` hasher.
//!
//! pymemcache's `RendezvousHash.get_node` scores every node with its pure-Python `murmur3_32` over
//! `f"{node}-{key}"` on every get, set and delete. [`rendezvous_node`] picks the same node for every
//! key: changing the pick would move every cached key to another server.
//!
//! pymemcache hashes a str, not bytes: each character contributes its code point's low 8 bits, and the
//! length is the character count. Its `ord(data[i + 3]) << 24` is unmasked, but only the low 32 bits
//! of each step reach the result, so the high bits never matter. [`hash_input`] reproduces that.

#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyList, PyString};

/// `MurmurHash3_x86_32`, as pymemcache's `murmur3_32` computes it over the bytes [`hash_input`] builds.
pub fn murmur3_32(data: &[u8], seed: u32) -> u32 {
    const C1: u32 = 0xCC9E_2D51;
    const C2: u32 = 0x1B87_3593;
    let mix = |k: u32| k.wrapping_mul(C1).rotate_left(15).wrapping_mul(C2);

    let mut h1 = seed;
    let (blocks, tail) = data.as_chunks::<4>();
    for block in blocks {
        h1 ^= mix(u32::from_le_bytes(*block));
        h1 = h1.rotate_left(13).wrapping_mul(5).wrapping_add(0xE654_6B64);
    }
    if !tail.is_empty() {
        let k1 = tail
            .iter()
            .rev()
            .fold(0u32, |k, &b| (k << 8) | u32::from(b));
        h1 ^= mix(k1);
    }

    // pymemcache XORs in len(data), a character count; only its low 32 bits survive the final mask.
    #[allow(clippy::cast_possible_truncation)]
    {
        h1 ^= data.len() as u32;
    }
    h1 ^= h1 >> 16;
    h1 = h1.wrapping_mul(0x85EB_CA6B);
    h1 ^= h1 >> 13;
    h1 = h1.wrapping_mul(0xC2B2_AE35);
    h1 ^ (h1 >> 16)
}

/// Writes `f"{node}-{key}"` into `buf` as pymemcache hashes it: one byte per character, the low 8 bits
/// of its code point.
pub fn hash_input(buf: &mut Vec<u8>, node: &str, key: &str) {
    buf.clear();
    for part in [node, "-", key] {
        if part.is_ascii() {
            buf.extend_from_slice(part.as_bytes());
        } else {
            // `as u8` keeps the low 8 bits, as pymemcache's `& 0xFF` does.
            #[allow(clippy::cast_possible_truncation)]
            buf.extend(part.chars().map(|c| c as u8));
        }
    }
}

/// The index of the node pymemcache's `RendezvousHash.get_node` returns, or None when `nodes` is
/// empty: the highest score wins, and a tie goes to the greater string. Rust orders `str` by its
/// UTF-8 bytes, which is code-point order, as Python orders `str`.
pub fn rendezvous_index<'a>(
    nodes: impl IntoIterator<Item = &'a str>,
    key: &str,
    seed: u32,
) -> Option<usize> {
    let mut buf = Vec::new();
    let mut best: Option<(u32, usize, &str)> = None;
    for (i, node) in nodes.into_iter().enumerate() {
        hash_input(&mut buf, node, key);
        let score = murmur3_32(&buf, seed);
        match best {
            Some((high, _, winner)) if score < high || (score == high && node <= winner) => {}
            _ => best = Some((score, i, node)),
        }
    }
    best.map(|(_, i, _)| i)
}

/// The node of `nodes` that pymemcache's `RendezvousHash.get_node` returns for `key`, the node object
/// itself, so a concurrent `remove_node` cannot shift it.
///
/// None when the caller must ask pymemcache instead: `nodes` is empty, or `key` or a node is not a
/// str (pymemcache formats a bytes key as `b'...'`, and a non-str node through `str()`).
#[cfg(feature = "python")]
#[pyfunction]
pub fn rendezvous_node<'py>(
    nodes: &Bound<'py, PyList>,
    key: &Bound<'py, PyAny>,
    seed: u32,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Ok(key) = key.cast::<PyString>() else {
        return Ok(None);
    };
    let key = key.to_str()?;
    // A snapshot: another thread may add or remove a node meanwhile, as in pymemcache's own loop.
    let nodes: Vec<Bound<'py, PyAny>> = nodes.iter().collect();
    let mut names = Vec::with_capacity(nodes.len());
    for node in &nodes {
        let Ok(name) = node.cast::<PyString>() else {
            return Ok(None);
        };
        names.push(name.to_str()?);
    }
    Ok(rendezvous_index(names.iter().copied(), key, seed).map(|i| nodes[i].clone()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hash_str(s: &str, seed: u32) -> u32 {
        let mut buf = Vec::new();
        hash_input(&mut buf, s, "");
        buf.pop(); // the "-" separator
        murmur3_32(&buf, seed)
    }

    #[test]
    fn murmur3_matches_reference_vectors() {
        // MurmurHash3_x86_32 reference values, which pymemcache's murmur3_32 also returns.
        assert_eq!(murmur3_32(b"", 0), 0);
        assert_eq!(murmur3_32(b"", 1), 0x514E_28B7);
        assert_eq!(murmur3_32(b"", 0xFFFF_FFFF), 0x81F1_6F39);
        assert_eq!(murmur3_32(b"\xff\xff\xff\xff", 0), 0x7629_3B50);
        assert_eq!(murmur3_32(b"!Ce\x87", 0), 0xF55B_516B);
        assert_eq!(murmur3_32(b"!Ce", 0), 0x7E4A_8634);
        assert_eq!(murmur3_32(b"!C", 0), 0xA0F7_B07A);
        assert_eq!(murmur3_32(b"!", 0), 0x7266_1CF4);
        assert_eq!(murmur3_32(b"Hello, world!", 1234), 0xFAF6_CDB3);
    }

    #[test]
    fn non_ascii_keeps_low_byte_per_character() {
        // U+0141 and U+0241 share their low byte with 'A' (0x41).
        assert_eq!(hash_str("\u{141}bc", 0), hash_str("Abc", 0));
        assert_eq!(hash_str("\u{241}\u{10041}", 7), hash_str("AA", 7));
    }

    #[test]
    fn score_tie_goes_to_the_greater_string() {
        // A murmur3 collision for key "k", found by brute force.
        let (low, high) = ("mc46898:11211", "mc61399:11211");
        assert_eq!(
            hash_str(&format!("{low}-k"), 0),
            hash_str(&format!("{high}-k"), 0)
        );
        assert_eq!(rendezvous_index([low, high], "k", 0), Some(1));
        assert_eq!(rendezvous_index([high, low], "k", 0), Some(0));
    }

    #[test]
    fn empty_nodes_pick_nothing_and_one_node_wins() {
        assert_eq!(rendezvous_index([], "k", 0), None);
        assert_eq!(rendezvous_index(["a:1"], "k", 0), Some(0));
    }
}
