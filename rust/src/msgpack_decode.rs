//! The MessagePack decode of StandardSerializer, AutoSerializer and interop reads: cachekit-core's
//! structure walk and the decode in one pass.
//!
//! Each returns what `msgpack.unpackb(data, raw=False, use_list=True, ...)` returns after
//! `check_msgpack_structure(data, max_depth)` passed, and raises the same exception class where either
//! refuses (`serializers/base.py::unpackb_bounded`):
//! - `unpackb_standard` and `unpackb_auto`: `strict_map_key=True, object_hook=hook`;
//! - `unpackb_interop`: `strict_map_key=False, object_pairs_hook=_build_map` (`interop.py`), whose map
//!   is `object_hook`'s on its `dict(pairs)`.
//!
//! The bounds are the walk's, applied at the same points: before a header's container is
//! allocated, its depth and the elements every open container still owes are checked against the
//! input left, so total pre-allocation stays bounded by the input length. Any failure re-runs the
//! walk over the whole input and raises its error if it has one, which keeps today's precedence:
//! a bound the walk would refuse anywhere in the document wins over a decode error before it.
//!
//! The hook runs only on a map holding a `str` key equal to one of its caller's marker keys. Any
//! other map it would return unchanged, because each marker test reads its key with `dict.get`.
//!
//! Interop keys (IOP-18) may be of any type. A map's `str` and `bytes` keys go straight into its dict:
//! they hash under the per-process secret, so no input can make that build quadratic. From its first
//! key of another type, the map's pairs are held in read order after the dict's (`hold`), and
//! `_build_map` makes the map from them at its close, as msgpack-python's `object_pairs_hook` did: it
//! counts how many such keys the map holds before it hashes any.
//!
//! Safe Rust only (`SECURITY.md`): every object is a PyO3 `Bound`, so each reference is released
//! by its owner on any exit. PyO3's plain constructors abort the process when CPython is out of
//! memory (the release profile sets `panic = "abort"`). A single allocation the input sizes, a
//! long array's list, a map's held pairs or the bytes of a str, bin, ext or `ExtraData`, goes
//! through a constructor that raises MemoryError instead (`nones`, `bytes_of`,
//! `PyString::from_bytes`). A dict, a list of at most `SHORT_LIST` items, a pair's tuple, a float
//! or an int outside -5..=256 does not: running out of memory while building one aborts the
//! process, where msgpack-python raises MemoryError.

use std::convert::Infallible;

use cachekit_core::check_msgpack_structure;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::short_bytes::{load8, same, word};

/// The keys `_standard_object_hook` (`standard_serializer.py`) and `_revive_sentinels` (`interop.py`)
/// test.
const TEMPORAL_MARKERS: &[&[u8]] = &[b"__datetime__", b"__date__", b"__time__"];
/// The keys `_auto_object_hook` (`auto_serializer.py`) tests.
const AUTO_MARKERS: &[&[u8]] = &[
    b"__datetime__",
    b"__date__",
    b"__time__",
    b"__uuid__",
    b"__tuple__",
    b"__set__",
    b"__ndarray__",
];

/// `Decoder::is_marker` tests `__` before the list, so every marker key must start with it.
const fn dunder_prefixed(keys: &[&[u8]]) -> bool {
    let mut i = 0;
    while i < keys.len() {
        if keys[i].len() < 2 || keys[i][0] != b'_' || keys[i][1] != b'_' {
            return false;
        }
        i += 1;
    }
    true
}
const _: () = assert!(dunder_prefixed(TEMPORAL_MARKERS) && dunder_prefixed(AUTO_MARKERS));

/// Strings at most this long go through a per-decode cache, so a repeated one is one object: map
/// keys in one table, values in another.
const KEY_CACHE_MAX_LEN: usize = 64;
const VALUE_CACHE_MAX_LEN: usize = 32;
const CACHE_BITS: u32 = 8;
const CACHE_SLOTS: usize = 1 << CACHE_BITS;

/// Lists at most this long (512 bytes of slots) are made by PyO3's plain constructor (`nones`).
const SHORT_LIST: u64 = 64;

/// A Python error is set (`restore` put it there, `PyErr::fetch` takes it), or the input broke a
/// bound. A fieldless enum, so a decode step's result stays two words.
enum Fail {
    Py,
    Bound,
}

/// An open container, filled as each element arrives.
enum Open<'a, 'py> {
    /// An array: its list, made with `None` in every slot, and the next index to fill.
    List(Bound<'py, PyList>, usize),
    /// A map: its dict, the key read, awaiting its value, and its held pairs once it has any.
    Map(
        Bound<'py, PyDict>,
        Option<Bound<'py, PyAny>>,
        Option<Held<'a, 'py>>,
    ),
}

/// A map's pairs from its first key neither `str` nor `bytes` on (interop only), for `build`.
struct Held<'a, 'py> {
    /// `(key, value)` tuples in read order, one slot per pair the map has left (`hold`): the
    /// dict's pairs, then every pair read since. Full when the map closes.
    pairs: Bound<'py, PyList>,
    /// The next slot to fill.
    next: usize,
    /// The caller's pairs hook, which makes the map from them at its close.
    build: &'a Bound<'py, PyAny>,
}

/// An open array or map.
struct Frame<'a, 'py> {
    open: Open<'a, 'py>,
    /// Elements still to read: items for an array, keys plus values for a map.
    owed: u64,
    /// Map: a `str` key equal to a marker key was read.
    marker: bool,
}

/// One cached string: its bytes' position in the input, and the `str`.
struct StrSlot<'py> {
    start: usize,
    len: usize,
    obj: Bound<'py, PyAny>,
}

struct Decoder<'a, 'py> {
    py: Python<'py>,
    data: &'a [u8],
    pos: usize,
    max_depth: usize,
    /// The keys the hook tests.
    markers: &'static [&'static [u8]],
    /// None: `strict_map_key=True`, a key must be exactly `str` or `bytes`. Some: any key, and a map
    /// holding one of another type is made by this pairs hook (see the module docs).
    pairs_hook: Option<&'a Bound<'py, PyAny>>,
    /// Elements owed across every open container, the root included (the walk's `pending`).
    pending: u64,
    stack: Vec<Frame<'a, 'py>>,
    /// Cached strings; a table slot holds an index into it plus one, or 0 when empty. A slot holds
    /// one string at a time, a colliding one replacing it, so this never exceeds `2 * CACHE_SLOTS`.
    cached: Vec<StrSlot<'py>>,
    keys: [u16; CACHE_SLOTS],
    values: [u16; CACHE_SLOTS],
}

/// The cache slot for `bytes`: its first and last eight bytes, multiplied and shifted.
#[inline]
fn slot_of(bytes: &[u8]) -> usize {
    let n = bytes.len();
    let head = if n >= 8 { load8(bytes, 0) } else { word(bytes) };
    let tail = if n > 8 { load8(bytes, n - 8) } else { 0 };
    let h = (head ^ tail.rotate_left(29) ^ (n as u64)).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    (h >> (64 - CACHE_BITS)) as usize
}

impl<'a, 'py> Decoder<'a, 'py> {
    fn new(
        py: Python<'py>,
        data: &'a [u8],
        max_depth: usize,
        markers: &'static [&'static [u8]],
        pairs_hook: Option<&'a Bound<'py, PyAny>>,
    ) -> Self {
        Self {
            py,
            data,
            pos: 0,
            max_depth,
            markers,
            pairs_hook,
            pending: 1,
            stack: Vec::new(),
            cached: Vec::new(),
            keys: [0; CACHE_SLOTS],
            values: [0; CACHE_SLOTS],
        }
    }

    /// The walk's per-element accounting, for an element whose prefix is consumed: `payload`
    /// bytes follow and it opens a container of `children` elements.
    #[inline]
    fn account(&mut self, payload: u64, children: u64) -> Result<(), Fail> {
        let remaining = (self.data.len() - self.pos) as u64;
        if payload > remaining {
            return Err(Fail::Bound);
        }
        self.pending += children;
        if self.pending > remaining - payload {
            return Err(Fail::Bound);
        }
        Ok(())
    }

    /// Consume `len` payload bytes `account` admitted.
    #[inline]
    fn take(&mut self, len: u64) -> &'a [u8] {
        let start = self.pos;
        // `account` checked len <= data.len() - pos, so this cannot truncate or overrun.
        self.pos += len as usize;
        &self.data[start..self.pos]
    }

    /// Whether the next element read is a map key.
    #[inline]
    fn at_key(&self) -> bool {
        matches!(
            self.stack.last(),
            Some(Frame {
                open: Open::Map(_, None, _),
                ..
            })
        )
    }

    /// Whether `key`, a `str` key's bytes, is one of the keys the hook tests.
    #[inline]
    fn is_marker(&self, key: &[u8]) -> bool {
        key.starts_with(b"__") && self.markers.contains(&key)
    }

    fn decode(&mut self, hook: &Bound<'py, PyAny>) -> Result<Bound<'py, PyAny>, Fail> {
        let py = self.py;
        loop {
            let marker = *self.data.get(self.pos).ok_or(Fail::Bound)?;
            self.pos += 1;
            self.pending -= 1;
            if let Some(f) = self.stack.last_mut() {
                f.owed -= 1;
            }
            let value = match marker {
                0x00..=0x7f => self.scalar(int(py, i64::from(marker)))?,
                0xe0..=0xff => self.scalar(int(py, i64::from(marker as i8)))?,
                0xc0 => self.scalar(py.None().into_bound(py))?,
                0xc2 => self.scalar(PyBool::new(py, false).to_owned().into_any())?,
                0xc3 => self.scalar(PyBool::new(py, true).to_owned().into_any())?,
                0xa0..=0xbf => self.string(u64::from(marker & 0x1f))?,
                0xd9 => self.prefixed(1).and_then(|n| self.string(n))?,
                0xda => self.prefixed(2).and_then(|n| self.string(n))?,
                0xdb => self.prefixed(4).and_then(|n| self.string(n))?,
                0xc4 => self.prefixed(1).and_then(|n| self.binary(n))?,
                0xc5 => self.prefixed(2).and_then(|n| self.binary(n))?,
                0xc6 => self.prefixed(4).and_then(|n| self.binary(n))?,
                0xc7 => self.prefixed(1).and_then(|n| self.ext(n))?,
                0xc8 => self.prefixed(2).and_then(|n| self.ext(n))?,
                0xc9 => self.prefixed(4).and_then(|n| self.ext(n))?,
                0xd4..=0xd8 => self.ext(1u64 << (marker - 0xd4))?,
                0xca..=0xd3 => self.number(marker)?,
                0x90..=0x9f => self.open(false, u64::from(marker & 0x0f))?,
                0xdc => self.prefixed(2).and_then(|n| self.open(false, n))?,
                0xdd => self.prefixed(4).and_then(|n| self.open(false, n))?,
                0x80..=0x8f => self.open(true, u64::from(marker & 0x0f))?,
                0xde => self.prefixed(2).and_then(|n| self.open(true, n))?,
                0xdf => self.prefixed(4).and_then(|n| self.open(true, n))?,
                0xc1 => return Err(Fail::Bound),
            };
            // None: a non-empty container was pushed, so read its first element.
            let Some(mut value) = value else {
                continue;
            };
            // Hand the value to its container, closing every container it completes.
            loop {
                let Some(f) = self.stack.last_mut() else {
                    return Ok(value);
                };
                match &mut f.open {
                    Open::List(list, index) => {
                        list.set_item(*index, value).map_err(|e| restore(py, e))?;
                        *index += 1;
                    }
                    Open::Map(dict, awaiting_key, held) => match awaiting_key.take() {
                        None => {
                            *awaiting_key = Some(value);
                            break;
                        }
                        Some(key) => {
                            if let Some(held) = held {
                                // Through the sequence protocol, as in `hold`.
                                PyTuple::new(py, [key, value])
                                    .and_then(|p| held.pairs.as_sequence().set_item(held.next, p))
                                    .map_err(|e| restore(py, e))?;
                                held.next += 1;
                            } else if key.is_exact_instance_of::<PyString>()
                                || key.is_exact_instance_of::<PyBytes>()
                            {
                                dict.set_item(key, value).map_err(|e| restore(py, e))?;
                            } else if let Some(build) = self.pairs_hook {
                                // `owed` counts the keys and values of the pairs still to read.
                                let (pairs, next) = hold(py, dict, key, value, f.owed / 2)
                                    .map_err(|e| restore(py, e))?;
                                *held = Some(Held { pairs, next, build });
                            } else {
                                // strict_map_key=True: a map key must be exactly `str` or `bytes`.
                                return Err(restore(py, key_error(&key)));
                            }
                        }
                    },
                }
                if f.owed > 0 {
                    break;
                }
                let done = self.stack.pop().expect("a frame was just read");
                value = match done.open {
                    Open::List(list, _) => list.into_any(),
                    // The pairs hook makes the map, revived as it is (`_build_map`), so the hook does not run.
                    Open::Map(_, _, Some(held)) => held
                        .build
                        .call1((held.pairs,))
                        .map_err(|e| restore(py, e))?,
                    Open::Map(dict, _, None) if done.marker => {
                        hook.call1((dict,)).map_err(|e| restore(py, e))?
                    }
                    Open::Map(dict, _, None) => dict.into_any(),
                };
            }
        }
    }

    /// A `width`-byte big-endian length prefix.
    #[inline]
    fn prefixed(&mut self, width: usize) -> Result<u64, Fail> {
        let end = self
            .pos
            .checked_add(width)
            .filter(|e| *e <= self.data.len())
            .ok_or(Fail::Bound)?;
        let n = big_endian(&self.data[self.pos..end]);
        self.pos = end;
        Ok(n)
    }

    /// A one-byte element, already built as `obj` (dropped on a bound failure).
    #[inline]
    fn scalar(&mut self, obj: Bound<'py, PyAny>) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        self.account(0, 0)?;
        Ok(Some(obj))
    }

    fn string(&mut self, len: u64) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        self.account(len, 0)?;
        let at_key = self.at_key();
        let start = self.pos;
        let bytes = self.take(len);
        if at_key && self.is_marker(bytes) {
            if let Some(f) = self.stack.last_mut() {
                f.marker = true;
            }
        }
        let max = if at_key {
            KEY_CACHE_MAX_LEN
        } else {
            VALUE_CACHE_MAX_LEN
        };
        if bytes.len() > max {
            return self.decode_utf8(bytes).map(Some);
        }
        let i = slot_of(bytes);
        let held = if at_key { self.keys[i] } else { self.values[i] };
        if let Some(s) = (held as usize).checked_sub(1).map(|j| &self.cached[j]) {
            if s.len == bytes.len() && same(&self.data[s.start..s.start + s.len], bytes) {
                return Ok(Some(s.obj.clone()));
            }
        }
        // Not interned, unlike msgpack-python's keys: equal either way, and interning a distinct key
        // costs more than every later lookup of it saves.
        let obj = self.decode_utf8(bytes)?;
        let slot = StrSlot {
            start,
            len: bytes.len(),
            obj: obj.clone(),
        };
        if held == 0 {
            self.cached.push(slot);
            // At most 2 * CACHE_SLOTS entries (see `cached`), so the index fits.
            *(if at_key {
                &mut self.keys[i]
            } else {
                &mut self.values[i]
            }) = self.cached.len() as u16;
        } else {
            self.cached[held as usize - 1] = slot;
        }
        Ok(Some(obj))
    }

    /// `bytes` as a `str`, or the UnicodeDecodeError of the strict UTF-8 decode msgpack-python runs.
    #[inline]
    fn decode_utf8(&self, bytes: &[u8]) -> Result<Bound<'py, PyAny>, Fail> {
        PyString::from_bytes(self.py, bytes)
            .map(Bound::into_any)
            .map_err(|e| restore(self.py, e))
    }

    fn binary(&mut self, len: u64) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        self.account(len, 0)?;
        let bytes = self.take(len);
        bytes_of(self.py, bytes)
            .map(|b| Some(b.into_any()))
            .map_err(|e| restore(self.py, e))
    }

    fn number(&mut self, marker: u8) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        let py = self.py;
        let width = 1u64 << (marker & 0x03);
        self.account(width, 0)?;
        let raw = big_endian(self.take(width));
        Ok(Some(match marker {
            0xca => PyFloat::new(py, f64::from(f32::from_bits(raw as u32))).into_any(),
            0xcb => PyFloat::new(py, f64::from_bits(raw)).into_any(),
            0xcf => int(py, raw),
            0xd0 => int(py, i64::from(raw as u8 as i8)),
            0xd1 => int(py, i64::from(raw as u16 as i16)),
            0xd2 => int(py, i64::from(raw as u32 as i32)),
            // 0xcc..=0xce (unsigned, at most 32 bits) and 0xd3 (signed 64 bits).
            _ => int(py, raw as i64),
        }))
    }

    /// An ext of `len` data bytes after its type byte, decoded as msgpack-python's defaults do:
    /// type -1 to `msgpack.Timestamp` (`timestamp`), any other to `msgpack.ExtType(code, data)`.
    /// cachekit never writes an ext, so this path is not hot.
    fn ext(&mut self, len: u64) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        let py = self.py;
        self.account(len + 1, 0)?;
        let code = self.take(1)[0] as i8;
        let data = self.take(len);
        let made = if code == -1 {
            timestamp(py, data)
        } else {
            bytes_of(py, data)
                .and_then(|data| msgpack_attr(py, "ExtType").and_then(|t| t.call1((code, data))))
        };
        made.map(Some).map_err(|e| restore(py, e))
    }

    /// Open a container of `n` elements (`2n` slots for a map). Returns None after pushing a
    /// non-empty one, or the empty container.
    fn open(&mut self, is_map: bool, n: u64) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        // An empty container is still a level, so depth is checked before the n > 0 push.
        if self.stack.len() >= self.max_depth {
            return Err(Fail::Bound);
        }
        let owed = if is_map { 2 * n } else { n };
        self.account(0, owed)?;
        if n == 0 {
            return Ok(Some(if is_map {
                PyDict::new(self.py).into_any()
            } else {
                PyList::empty(self.py).into_any()
            }));
        }
        let open = if is_map {
            Open::Map(PyDict::new(self.py), None, None)
        } else {
            Open::List(nones(self.py, n).map_err(|e| restore(self.py, e))?, 0)
        };
        self.stack.push(Frame {
            open,
            owed,
            marker: false,
        });
        Ok(None)
    }
}

/// A list of `n` `None`s, which the decode replaces in order. `n` is at most the input length,
/// so a list longer than `SHORT_LIST` is made as `[None] * n`, which raises MemoryError where
/// PyO3's list constructor would abort. A shorter one, at most 512 bytes of slots, comes from
/// that constructor: making it by repeat would add about 1% to a small document's decode.
fn nones(py: Python<'_>, n: u64) -> PyResult<Bound<'_, PyList>> {
    let none = py.None().into_bound(py);
    if n <= SHORT_LIST {
        return PyList::new(py, std::iter::repeat_n(none, n as usize));
    }
    let seed = PyList::new(py, [none])?;
    Ok(seed
        .as_sequence()
        .repeat(n as usize)?
        .cast_into::<PyList>()?)
}

/// A Python `int`.
#[inline]
fn int<'py>(
    py: Python<'py>,
    n: impl IntoPyObject<'py, Output = Bound<'py, PyInt>, Error = Infallible>,
) -> Bound<'py, PyAny> {
    let Ok(n) = n.into_pyobject(py);
    n.into_any()
}

/// `bytes` holding `data`. `new_with`, not `new`: it raises MemoryError where `new` would abort,
/// at the cost of zeroing the buffer before the copy.
fn bytes_of<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    PyBytes::new_with(py, data.len(), |b| {
        b.copy_from_slice(data);
        Ok(())
    })
}

#[inline]
fn big_endian(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0u64, |acc, b| (acc << 8) | u64::from(*b))
}

/// `msgpack.Timestamp(seconds, nanoseconds)` from a type -1 ext's data, read and checked as
/// msgpack-python's C unpacker does (`unpack_timestamp`), with its error messages: interop's
/// InteropDecodeError quotes them. `Timestamp.from_bytes` words both refusals differently.
fn timestamp<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let (seconds, nanoseconds) = match data.len() {
        4 => (big_endian(data) as i64, 0),
        8 => {
            let packed = big_endian(data);
            ((packed & 0x3_ffff_ffff) as i64, packed >> 34)
        }
        // A signed 64-bit count of seconds after the nanoseconds.
        12 => (big_endian(&data[4..]) as i64, big_endian(&data[..4])),
        n => {
            return Err(PyValueError::new_err(format!(
                "invalid timestamp data (length {n})"
            )))
        }
    };
    if nanoseconds > 999_999_999 {
        return Err(PyValueError::new_err(
            "nanoseconds must be a non-negative integer not greater than 999999999.",
        ));
    }
    msgpack_attr(py, "Timestamp")?.call1((seconds, nanoseconds))
}

/// Set `e` as the current Python error, for the `Fail::Py` path to fetch.
fn restore(py: Python<'_>, e: PyErr) -> Fail {
    e.restore(py);
    Fail::Py
}

/// msgpack-python's `strict_map_key=True` refusal of `key`.
fn key_error(key: &Bound<'_, PyAny>) -> PyErr {
    let name = key
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_else(|_| "object".to_owned());
    PyValueError::new_err(format!(
        "{name:.100} is not allowed for map key when strict_map_key=True"
    ))
}

/// A map's held pairs as it reads its first key neither `str` nor `bytes`, and the next slot to
/// fill: `dict`'s pairs, in the order they were read, then that one, in a list with a slot for
/// each of the `rest` pairs still to read. A `dict` keeps a repeated key where it was first read,
/// with its last value, as `dict(pairs)` would. `dict` is emptied, so it holds no second copy.
///
/// The slots are set through the sequence protocol, not `PyList::set_item`: with a second caller,
/// LLVM stops inlining that into the decode loop, and a 79-byte StandardSerializer decode ran 3.5%
/// more instructions on CPython 3.12.
fn hold<'py>(
    py: Python<'py>,
    dict: &Bound<'py, PyDict>,
    key: Bound<'py, PyAny>,
    value: Bound<'py, PyAny>,
    rest: u64,
) -> PyResult<(Bound<'py, PyList>, usize)> {
    // At most the map's pairs, so at most the input length.
    let pairs = nones(py, dict.len() as u64 + 1 + rest)?;
    let slots = pairs.as_sequence();
    let mut next = 0;
    // Each pair as the `(key, value)` tuple msgpack-python hands a pairs hook.
    for (k, v) in dict.iter() {
        slots.set_item(next, PyTuple::new(py, [k, v])?)?;
        next += 1;
    }
    slots.set_item(next, PyTuple::new(py, [key, value])?)?;
    dict.clear();
    Ok((pairs, next + 1))
}

fn msgpack_attr<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    static MSGPACK: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
    MSGPACK
        .get_or_try_init(py, || py.import("msgpack").map(Bound::unbind))?
        .bind(py)
        .getattr(name)
}

/// Each reader's marker keys, as `str`s, by reader: `MARKER_KEYS` in the extension, which a test
/// compares with the keys each Python hook tests, so a key added to a hook but not here fails it.
pub fn marker_keys(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let keys = PyDict::new(py);
    for (reader, markers) in [
        ("standard", TEMPORAL_MARKERS),
        ("auto", AUTO_MARKERS),
        ("interop", TEMPORAL_MARKERS),
    ] {
        let names = markers.iter().map(|k| String::from_utf8_lossy(k));
        keys.set_item(reader, PyTuple::new(py, names)?)?;
    }
    Ok(keys)
}

/// Decode `data` as `unpackb_bounded(data, raw=False, use_list=True, object_hook=hook)` does
/// (see the module docs), calling `hook` only on maps holding a `__datetime__`, `__date__` or
/// `__time__` key: StandardSerializer's decode.
pub fn unpackb_standard(
    py: Python<'_>,
    data: &[u8],
    max_depth: usize,
    hook: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    unpackb(py, data, max_depth, TEMPORAL_MARKERS, hook, None)
}

/// [`unpackb_standard`] for AutoSerializer, whose hook also tests `__uuid__`, `__tuple__`, `__set__`
/// and `__ndarray__`.
pub fn unpackb_auto(
    py: Python<'_>,
    data: &[u8],
    max_depth: usize,
    hook: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    unpackb(py, data, max_depth, AUTO_MARKERS, hook, None)
}

/// Decode `data` as `unpackb_bounded(data, raw=False, strict_map_key=False,
/// object_pairs_hook=pairs_hook)` does, where `pairs_hook` builds a map and returns
/// `hook(dict(pairs))` (`interop.py`'s `_build_map` and `_revive_sentinels`). A map whose keys are all
/// `str` or `bytes` is built here and goes to `hook` only if it holds a `__datetime__`, `__date__` or
/// `__time__` key; one holding any other key goes to `pairs_hook` (see the module docs).
pub fn unpackb_interop(
    py: Python<'_>,
    data: &[u8],
    max_depth: usize,
    hook: &Bound<'_, PyAny>,
    pairs_hook: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    unpackb(
        py,
        data,
        max_depth,
        TEMPORAL_MARKERS,
        hook,
        Some(pairs_hook),
    )
}

fn unpackb<'py>(
    py: Python<'py>,
    data: &[u8],
    max_depth: usize,
    markers: &'static [&'static [u8]],
    hook: &Bound<'py, PyAny>,
    pairs_hook: Option<&Bound<'py, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let mut decoder = Decoder::new(py, data, max_depth, markers, pairs_hook);
    let outcome = decoder.decode(hook);
    let consumed = decoder.pos;
    drop(decoder);
    let failure = match outcome {
        Ok(value) => {
            if consumed == data.len() {
                return Ok(value.unbind());
            }
            let extra = msgpack_attr(py, "ExtraData")?;
            return Err(PyErr::from_value(
                extra.call1((value, bytes_of(py, &data[consumed..])?))?,
            ));
        }
        Err(Fail::Py) => PyErr::fetch(py),
        // Only reachable if this decoder's inline accounting drifts from the walk: the walk below
        // then accepts what the decode refused. Fails closed, without the guard's own prefix, so a
        // drift cannot pass for a refused hostile document.
        Err(Fail::Bound) => PyValueError::new_err(
            "MessagePack decode bound disagrees with the structure walk (internal error)",
        ),
    };
    // The walk's verdict wins over anything the decode raised, as it does when it runs first.
    check_msgpack_structure(data, max_depth).map_err(structure_error)?;
    Err(failure)
}

/// The ValueError for a document `check_msgpack_structure` refused: `Unpack failed: MessagePack
/// document <what>`, the text the bounds tests pin as the guard's own.
pub(crate) fn structure_error(what: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(format!("Unpack failed: MessagePack document {what}"))
}
