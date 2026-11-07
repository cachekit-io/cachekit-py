//! Short byte slices read as words, for the MessagePack decode's string cache (`msgpack_decode`).
//!
//! The cache hashes and compares each short string it reads. Copying a slice of run-time length
//! into a word, or comparing two with `==`, compiles to a call to `memcpy` or `memcmp`: together
//! about 35 instructions per string. These read the bytes with fixed-width loads that overlap
//! instead, so they make no call.

/// The four bytes of `s` at `at`, as one load.
#[inline]
pub(crate) fn load4(s: &[u8], at: usize) -> u32 {
    let mut w = [0u8; 4];
    w.copy_from_slice(&s[at..at + 4]);
    u32::from_le_bytes(w)
}

/// The eight bytes of `s` at `at`, as one load.
#[inline]
pub(crate) fn load8(s: &[u8], at: usize) -> u64 {
    let mut w = [0u8; 8];
    w.copy_from_slice(&s[at..at + 8]);
    u64::from_le_bytes(w)
}

/// `b`, at most eight bytes, as a little-endian word padded with zeros. From four bytes on, two
/// loads that overlap where `b` is shorter than eight; under four, its first, middle and last byte.
#[inline]
pub(crate) fn word(b: &[u8]) -> u64 {
    let n = b.len();
    if n >= 4 {
        u64::from(load4(b, 0)) | u64::from(load4(b, n - 4)) << ((n - 4) * 8)
    } else if n > 0 {
        u64::from(b[0]) | u64::from(b[n / 2]) << (n / 2 * 8) | u64::from(b[n - 1]) << ((n - 1) * 8)
    } else {
        0
    }
}

/// Whether `a` and `b`, which are one length, hold the same bytes. Up to 16 bytes, by two loads
/// that overlap and together cover every byte (under four, its first, middle and last byte); past
/// that, `==`.
#[inline]
pub(crate) fn same(a: &[u8], b: &[u8]) -> bool {
    debug_assert_eq!(a.len(), b.len());
    let n = a.len();
    match n {
        0 => true,
        1..=3 => a[0] == b[0] && a[n / 2] == b[n / 2] && a[n - 1] == b[n - 1],
        4..=8 => load4(a, 0) == load4(b, 0) && load4(a, n - 4) == load4(b, n - 4),
        9..=16 => load8(a, 0) == load8(b, 0) && load8(a, n - 8) == load8(b, n - 8),
        _ => a == b,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `n` distinct bytes, so every position holds a value no other position does.
    fn bytes(n: usize) -> Vec<u8> {
        (0..n)
            .map(|i| (i as u8).wrapping_mul(37).wrapping_add(11))
            .collect()
    }

    #[test]
    fn word_is_the_zero_padded_little_endian_word() {
        for n in 0..=8 {
            let b = bytes(n);
            let mut padded = [0u8; 8];
            padded[..n].copy_from_slice(&b);
            assert_eq!(word(&b), u64::from_le_bytes(padded), "length {n}");
        }
    }

    #[test]
    fn same_agrees_with_eq_whatever_byte_differs() {
        // Through the first two arms past 16, so the `==` arm is checked too.
        for n in 0..=20 {
            let a = bytes(n);
            assert!(same(&a, &a.clone()), "length {n}");
            for at in 0..n {
                for flip in [0x01, 0x80, 0xff] {
                    let mut b = a.clone();
                    b[at] ^= flip;
                    assert!(!same(&a, &b), "length {n}, byte {at}, flip {flip:#x}");
                }
            }
        }
    }
}
