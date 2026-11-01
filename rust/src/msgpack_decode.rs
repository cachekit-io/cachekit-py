//! StandardSerializer's MessagePack decode: cachekit-core's structure walk and the decode in one pass.
//!
//! It returns what `msgpack.unpackb(data, raw=False, use_list=True, strict_map_key=True,
//! object_hook=hook)` returns after `check_msgpack_structure(data, max_depth)` passed, and raises
//! the same exception class where either refuses (`serializers/base.py::unpackb_bounded`).
//!
//! The bounds are the walk's, applied at the same points: before a header's container is
//! allocated, its depth and the elements every open container still owes are checked against the
//! input left, so total pre-allocation stays bounded by the input length. Any failure re-runs the
//! walk over the whole input and raises its error if it has one, which keeps today's precedence:
//! a bound the walk would refuse anywhere in the document wins over a decode error before it.
//!
//! The hook runs only on a map holding a `str` key equal to a marker key. Any other map it would
//! return unchanged, because each marker test reads its key with `dict.get`.
//!
//! Safe Rust only (`SECURITY.md`): every object is a PyO3 `Bound`, so each reference is released
//! by its owner on any exit, and an array's items wait on a stack until it closes, so no
//! partly-filled list is ever built.

use cachekit_core::check_msgpack_structure;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString};

/// The keys `_standard_object_hook` tests (`standard_serializer.py`).
const MARKER_KEYS: [&[u8]; 3] = [b"__datetime__", b"__date__", b"__time__"];

/// Strings at most this long go through a per-decode cache, so a repeated one is one object: map
/// keys in one table, values in another.
const KEY_CACHE_MAX_LEN: usize = 64;
const VALUE_CACHE_MAX_LEN: usize = 32;
const CACHE_BITS: u32 = 8;
const CACHE_SLOTS: usize = 1 << CACHE_BITS;

/// A Python error is set (`restore` put it there, `PyErr::fetch` takes it), or the input broke a
/// bound. A unit enum, so a decode step's result stays two words.
enum Fail {
    Py,
    Bound,
}

/// An open array or map.
struct Frame<'py> {
    /// Map: the dict, filled as each value arrives. Array: None.
    dict: Option<Bound<'py, PyDict>>,
    /// Map: the key read, awaiting its value.
    key: Option<Bound<'py, PyAny>>,
    /// Array: where its items start on the decoder's `items` stack.
    base: usize,
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
    /// Elements owed across every open container, the root included (the walk's `pending`).
    pending: u64,
    stack: Vec<Frame<'py>>,
    /// The items of every open array, innermost last; an array's become its list when it closes.
    /// Only decoded elements land here, so it never outgrows the input.
    items: Vec<Bound<'py, PyAny>>,
    /// Cached strings; a table slot holds an index into it plus one, or 0 when empty. A slot holds
    /// one string at a time, a colliding one replacing it, so this never exceeds `2 * CACHE_SLOTS`.
    cached: Vec<StrSlot<'py>>,
    keys: [u16; CACHE_SLOTS],
    values: [u16; CACHE_SLOTS],
}

/// The cache slot for `bytes`: its first and last eight bytes, multiplied and shifted.
#[inline]
fn slot_of(bytes: &[u8]) -> usize {
    fn word(b: &[u8]) -> u64 {
        let mut w = [0u8; 8];
        w[..b.len()].copy_from_slice(b);
        u64::from_le_bytes(w)
    }
    let n = bytes.len();
    let head = word(&bytes[..n.min(8)]);
    let tail = if n > 8 { word(&bytes[n - 8..]) } else { 0 };
    let h = (head ^ tail.rotate_left(29) ^ (n as u64)).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    (h >> (64 - CACHE_BITS)) as usize
}

impl<'a, 'py> Decoder<'a, 'py> {
    fn new(py: Python<'py>, data: &'a [u8], max_depth: usize) -> Self {
        Self {
            py,
            data,
            pos: 0,
            max_depth,
            pending: 1,
            stack: Vec::new(),
            items: Vec::new(),
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
        matches!(self.stack.last(), Some(f) if f.dict.is_some() && f.key.is_none())
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
                match &f.dict {
                    None => self.items.push(value),
                    Some(dict) => match f.key.take() {
                        None => {
                            f.key = Some(value);
                            break;
                        }
                        Some(key) => {
                            // strict_map_key=True: a map key must be exactly `str` or `bytes`.
                            if !(key.is_exact_instance_of::<PyString>()
                                || key.is_exact_instance_of::<PyBytes>())
                            {
                                return Err(restore(py, key_error(&key)));
                            }
                            dict.set_item(key, value).map_err(|e| restore(py, e))?;
                        }
                    },
                }
                if f.owed > 0 {
                    break;
                }
                let done = self.stack.pop().expect("a frame was just read");
                value = match done.dict {
                    Some(dict) => dict.into_any(),
                    None => PyList::new(py, self.items.drain(done.base..))
                        .map_err(|e| restore(py, e))?
                        .into_any(),
                };
                if done.marker {
                    value = hook.call1((value,)).map_err(|e| restore(py, e))?;
                }
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
        if at_key && MARKER_KEYS.contains(&bytes) {
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
            if s.len == bytes.len() && &self.data[s.start..s.start + s.len] == bytes {
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
        Ok(Some(PyBytes::new(self.py, bytes).into_any()))
    }

    fn number(&mut self, marker: u8) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        let py = self.py;
        let width = 1u64 << (marker & 0x03);
        self.account(width, 0)?;
        let raw = big_endian(self.take(width));
        Ok(Some(match marker {
            0xca => PyFloat::new(py, f64::from(f32::from_bits(raw as u32))).into_any(),
            0xcb => PyFloat::new(py, f64::from_bits(raw)).into_any(),
            0xcf => match raw.into_pyobject(py) {
                Ok(n) => n.into_any(),
                Err(never) => match never {},
            },
            0xd0 => int(py, i64::from(raw as u8 as i8)),
            0xd1 => int(py, i64::from(raw as u16 as i16)),
            0xd2 => int(py, i64::from(raw as u32 as i32)),
            // 0xcc..=0xce (unsigned, at most 32 bits) and 0xd3 (signed 64 bits).
            _ => int(py, raw as i64),
        }))
    }

    /// An ext of `len` data bytes after its type byte, decoded as msgpack-python's defaults do:
    /// type -1 to `msgpack.Timestamp` (through its own `from_bytes`), any other to
    /// `msgpack.ExtType(code, data)`. cachekit never writes an ext, so this path is not hot.
    fn ext(&mut self, len: u64) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        let py = self.py;
        self.account(len + 1, 0)?;
        let code = self.take(1)[0] as i8;
        let data = self.take(len);
        let made = if code == -1 {
            msgpack_attr(py, "Timestamp")
                .and_then(|t| t.call_method1("from_bytes", (PyBytes::new(py, data),)))
        } else {
            msgpack_attr(py, "ExtType").and_then(|t| t.call1((code, PyBytes::new(py, data))))
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
        self.stack.push(Frame {
            dict: is_map.then(|| PyDict::new(self.py)),
            key: None,
            base: self.items.len(),
            owed,
            marker: false,
        });
        Ok(None)
    }
}

/// A Python `int`.
#[inline]
fn int(py: Python<'_>, n: i64) -> Bound<'_, PyAny> {
    match n.into_pyobject(py) {
        Ok(n) => n.into_any(),
        Err(never) => match never {},
    }
}

#[inline]
fn big_endian(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0u64, |acc, b| (acc << 8) | u64::from(*b))
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

fn msgpack_attr<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    static MSGPACK: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
    MSGPACK
        .get_or_try_init(py, || py.import("msgpack").map(Bound::unbind))?
        .bind(py)
        .getattr(name)
}

/// Decode `data` as `unpackb_bounded(data, raw=False, use_list=True, object_hook=hook)` does
/// (see the module docs), calling `hook` only on maps holding a marker key.
pub fn unpackb_standard(
    py: Python<'_>,
    data: &[u8],
    max_depth: usize,
    hook: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let mut decoder = Decoder::new(py, data, max_depth);
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
                extra.call1((value, PyBytes::new(py, &data[consumed..])))?,
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
