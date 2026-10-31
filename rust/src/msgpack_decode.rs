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

use std::os::raw::{c_char, c_long};
use std::ptr;

use cachekit_core::check_msgpack_structure;
use pyo3::exceptions::PyValueError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyBytes;

/// The keys `_standard_object_hook` tests (`standard_serializer.py`).
const MARKER_KEYS: [&[u8]; 3] = [b"__datetime__", b"__date__", b"__time__"];

/// Strings at most this long go through a per-decode cache, so a repeated one is one object: map
/// keys in one table, values in another.
const KEY_CACHE_MAX_LEN: usize = 64;
const VALUE_CACHE_MAX_LEN: usize = 32;
const CACHE_BITS: u32 = 8;
const CACHE_SLOTS: usize = 1 << CACHE_BITS;

/// A Python error is already set (`PyErr::fetch` takes it), or the input broke a bound.
enum Fail {
    Py,
    Bound,
}

/// An open array or map, holding a strong reference to its container.
struct Frame {
    obj: *mut ffi::PyObject,
    /// Elements still to read: items for an array, keys plus values for a map.
    owed: u64,
    /// Array: next index. Map: unused.
    index: ffi::Py_ssize_t,
    /// Map: the key read, awaiting its value (strong reference), or null.
    key: *mut ffi::PyObject,
    is_map: bool,
    /// Map: a `str` key equal to a marker key was read.
    marker: bool,
}

/// One cached string: its bytes' position in the input, and the `str` (a strong reference).
#[derive(Clone, Copy)]
struct StrSlot {
    start: usize,
    len: usize,
    obj: *mut ffi::PyObject,
}

struct Decoder<'a> {
    data: &'a [u8],
    pos: usize,
    max_depth: usize,
    /// Elements owed across every open container, the root included (the walk's `pending`).
    pending: u64,
    stack: Vec<Frame>,
    /// Cached strings; a table slot holds an index into it plus one, or 0 when empty. A slot holds
    /// one string at a time, a colliding one replacing it, so this never exceeds `2 * CACHE_SLOTS`.
    cached: Vec<StrSlot>,
    keys: [u16; CACHE_SLOTS],
    values: [u16; CACHE_SLOTS],
}

impl Drop for Decoder<'_> {
    fn drop(&mut self) {
        // SAFETY: every pointer here is a strong reference this decoder owns, or null; the
        // thread is attached (the decoder never outlives the pyfunction call).
        unsafe {
            for f in self.stack.drain(..) {
                ffi::Py_XDECREF(f.key);
                ffi::Py_DECREF(f.obj);
            }
            for slot in &self.cached {
                ffi::Py_DECREF(slot.obj);
            }
        }
    }
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

#[inline]
unsafe fn decode_utf8(bytes: &[u8]) -> *mut ffi::PyObject {
    ffi::PyUnicode_DecodeUTF8(
        bytes.as_ptr().cast::<c_char>(),
        bytes.len() as ffi::Py_ssize_t,
        ptr::null(),
    )
}

impl<'a> Decoder<'a> {
    fn new(data: &'a [u8], max_depth: usize) -> Self {
        Self {
            data,
            pos: 0,
            max_depth,
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
        matches!(self.stack.last(), Some(f) if f.is_map && f.key.is_null())
    }

    fn decode(
        &mut self,
        py: Python<'_>,
        hook: &Bound<'_, PyAny>,
    ) -> Result<*mut ffi::PyObject, Fail> {
        loop {
            let marker = *self.data.get(self.pos).ok_or(Fail::Bound)?;
            self.pos += 1;
            self.pending -= 1;
            if let Some(f) = self.stack.last_mut() {
                f.owed -= 1;
            }
            // SAFETY: the thread is attached; every constructor returns a new reference or null
            // with an exception set, which `check` turns into Fail::Py.
            let mut value = unsafe {
                match marker {
                    0x00..=0x7f => self.scalar(ffi::PyLong_FromLong(c_long::from(marker)))?,
                    0xe0..=0xff => self.scalar(ffi::PyLong_FromLong(c_long::from(marker as i8)))?,
                    0xc0 => self.scalar(ffi::Py_NewRef(ffi::Py_None()))?,
                    0xc2 => self.scalar(ffi::Py_NewRef(ffi::Py_False()))?,
                    0xc3 => self.scalar(ffi::Py_NewRef(ffi::Py_True()))?,
                    0xa0..=0xbf => self.string(u64::from(marker & 0x1f))?,
                    0xd9 => self.prefixed(1).and_then(|n| self.string(n))?,
                    0xda => self.prefixed(2).and_then(|n| self.string(n))?,
                    0xdb => self.prefixed(4).and_then(|n| self.string(n))?,
                    0xc4 => self.prefixed(1).and_then(|n| self.binary(n))?,
                    0xc5 => self.prefixed(2).and_then(|n| self.binary(n))?,
                    0xc6 => self.prefixed(4).and_then(|n| self.binary(n))?,
                    0xc7 => self.prefixed(1).and_then(|n| self.ext(py, n))?,
                    0xc8 => self.prefixed(2).and_then(|n| self.ext(py, n))?,
                    0xc9 => self.prefixed(4).and_then(|n| self.ext(py, n))?,
                    0xd4..=0xd8 => self.ext(py, 1u64 << (marker - 0xd4))?,
                    0xca..=0xd3 => self.number(marker)?,
                    0x90..=0x9f => self.open(false, u64::from(marker & 0x0f))?,
                    0xdc => self.prefixed(2).and_then(|n| self.open(false, n))?,
                    0xdd => self.prefixed(4).and_then(|n| self.open(false, n))?,
                    0x80..=0x8f => self.open(true, u64::from(marker & 0x0f))?,
                    0xde => self.prefixed(2).and_then(|n| self.open(true, n))?,
                    0xdf => self.prefixed(4).and_then(|n| self.open(true, n))?,
                    0xc1 => return Err(Fail::Bound),
                }
            };
            // A non-empty container was pushed: read its first element.
            if value.is_null() {
                continue;
            }
            // Hand the value to its container, closing every container it completes.
            loop {
                let Some(f) = self.stack.last_mut() else {
                    return Ok(value);
                };
                // SAFETY: `f.obj` is a strong reference to a list or dict this decoder built;
                // `value` and `f.key` are strong references the set calls consume.
                unsafe {
                    if !f.is_map {
                        if ffi::PyList_SetItem(f.obj, f.index, value) != 0 {
                            return Err(Fail::Py);
                        }
                        f.index += 1;
                    } else if f.key.is_null() {
                        f.key = value;
                        break;
                    } else {
                        let key = std::mem::replace(&mut f.key, ptr::null_mut());
                        let ok = key_admitted(key);
                        let rc = if ok {
                            ffi::PyDict_SetItem(f.obj, key, value)
                        } else {
                            0
                        };
                        if !ok {
                            set_key_error(py, key);
                        }
                        ffi::Py_DECREF(key);
                        ffi::Py_DECREF(value);
                        if !ok || rc != 0 {
                            return Err(Fail::Py);
                        }
                    }
                    if f.owed > 0 {
                        break;
                    }
                    let done = self.stack.pop().expect("a frame was just read");
                    value = done.obj;
                    if done.marker {
                        let obj = Bound::from_owned_ptr(py, value);
                        value = hook.call1((obj,)).map_err(|e| restore(py, e))?.into_ptr();
                    }
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

    /// A one-byte element, already built as `obj`.
    #[inline]
    fn scalar(&mut self, obj: *mut ffi::PyObject) -> Result<*mut ffi::PyObject, Fail> {
        // Accounting first: on a bound failure the object is dropped, not leaked.
        if let Err(e) = self.account(0, 0) {
            // SAFETY: obj is a new reference or null.
            unsafe { ffi::Py_XDECREF(obj) };
            return Err(e);
        }
        check(obj)
    }

    unsafe fn string(&mut self, len: u64) -> Result<*mut ffi::PyObject, Fail> {
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
            return check(decode_utf8(bytes));
        }
        let i = slot_of(bytes);
        let held = if at_key { self.keys[i] } else { self.values[i] };
        if let Some(s) = (held as usize).checked_sub(1).map(|j| self.cached[j]) {
            if s.len == bytes.len() && &self.data[s.start..s.start + s.len] == bytes {
                return Ok(ffi::Py_NewRef(s.obj));
            }
        }
        // Not interned, unlike msgpack-python's keys: equal either way, and interning a distinct key
        // costs more than every later lookup of it saves.
        let obj = check(decode_utf8(bytes))?;
        let slot = StrSlot {
            start,
            len: bytes.len(),
            obj: ffi::Py_NewRef(obj),
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
            let old = std::mem::replace(&mut self.cached[held as usize - 1], slot);
            ffi::Py_DECREF(old.obj);
        }
        Ok(obj)
    }

    unsafe fn binary(&mut self, len: u64) -> Result<*mut ffi::PyObject, Fail> {
        self.account(len, 0)?;
        let bytes = self.take(len);
        check(ffi::PyBytes_FromStringAndSize(
            bytes.as_ptr().cast::<c_char>(),
            bytes.len() as ffi::Py_ssize_t,
        ))
    }

    unsafe fn number(&mut self, marker: u8) -> Result<*mut ffi::PyObject, Fail> {
        let width = 1u64 << (marker & 0x03);
        self.account(width, 0)?;
        let raw = big_endian(self.take(width));
        check(match marker {
            0xca => ffi::PyFloat_FromDouble(f64::from(f32::from_bits(raw as u32))),
            0xcb => ffi::PyFloat_FromDouble(f64::from_bits(raw)),
            0xcc..=0xce => ffi::PyLong_FromLongLong(raw as i64),
            0xcf => ffi::PyLong_FromUnsignedLongLong(raw),
            0xd0 => ffi::PyLong_FromLong(c_long::from(raw as u8 as i8)),
            0xd1 => ffi::PyLong_FromLong(c_long::from(raw as u16 as i16)),
            0xd2 => ffi::PyLong_FromLong(c_long::from(raw as u32 as i32)),
            _ => ffi::PyLong_FromLongLong(raw as i64),
        })
    }

    /// An ext of `len` data bytes after its type byte, decoded as msgpack-python's defaults do:
    /// type -1 to `msgpack.Timestamp` (through its own `from_bytes`), any other to
    /// `msgpack.ExtType(code, data)`. cachekit never writes an ext, so this path is not hot.
    unsafe fn ext(&mut self, py: Python<'_>, len: u64) -> Result<*mut ffi::PyObject, Fail> {
        self.account(len + 1, 0)?;
        let code = self.take(1)[0] as i8;
        let data = self.take(len);
        let made = if code == -1 {
            msgpack_attr(py, "Timestamp")
                .and_then(|t| t.call_method1("from_bytes", (PyBytes::new(py, data),)))
        } else {
            msgpack_attr(py, "ExtType").and_then(|t| t.call1((code, PyBytes::new(py, data))))
        };
        made.map(Bound::into_ptr).map_err(|e| restore(py, e))
    }

    /// Open a container of `n` elements (`2n` slots for a map). Returns null after pushing a
    /// non-empty one, or the empty container.
    unsafe fn open(&mut self, is_map: bool, n: u64) -> Result<*mut ffi::PyObject, Fail> {
        // An empty container is still a level, so depth is checked before the n > 0 push.
        if self.stack.len() >= self.max_depth {
            return Err(Fail::Bound);
        }
        let owed = if is_map { 2 * n } else { n };
        self.account(0, owed)?;
        // n <= the input length now, so it fits Py_ssize_t.
        let obj = check(if is_map {
            ffi::PyDict_New()
        } else {
            ffi::PyList_New(n as ffi::Py_ssize_t)
        })?;
        if n == 0 {
            return Ok(obj);
        }
        self.stack.push(Frame {
            obj,
            owed,
            index: 0,
            key: ptr::null_mut(),
            is_map,
            marker: false,
        });
        Ok(ptr::null_mut())
    }
}

#[inline]
fn big_endian(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0u64, |acc, b| (acc << 8) | u64::from(*b))
}

#[inline]
fn check(obj: *mut ffi::PyObject) -> Result<*mut ffi::PyObject, Fail> {
    if obj.is_null() {
        Err(Fail::Py)
    } else {
        Ok(obj)
    }
}

/// Put a fetched error back so the `Fail::Py` path finds it.
fn restore(py: Python<'_>, e: PyErr) -> Fail {
    e.restore(py);
    Fail::Py
}

/// `strict_map_key=True`: a map key must be exactly `str` or `bytes`.
unsafe fn key_admitted(key: *mut ffi::PyObject) -> bool {
    ffi::PyUnicode_CheckExact(key) != 0 || ffi::PyBytes_CheckExact(key) != 0
}

unsafe fn set_key_error(py: Python<'_>, key: *mut ffi::PyObject) {
    let name = Bound::from_borrowed_ptr(py, key)
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_else(|_| "object".to_owned());
    PyValueError::new_err(format!(
        "{name:.100} is not allowed for map key when strict_map_key=True"
    ))
    .restore(py);
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
    let mut decoder = Decoder::new(data, max_depth);
    let outcome = decoder.decode(py, hook);
    let consumed = decoder.pos;
    drop(decoder);
    let failure = match outcome {
        Ok(value) => {
            // SAFETY: a strong reference the decode returned.
            let value = unsafe { Bound::from_owned_ptr(py, value) };
            if consumed == data.len() {
                return Ok(value.unbind());
            }
            let extra = msgpack_attr(py, "ExtraData")?;
            return Err(PyErr::from_value(
                extra.call1((value, PyBytes::new(py, &data[consumed..])))?,
            ));
        }
        Err(Fail::Py) => PyErr::fetch(py),
        Err(Fail::Bound) => {
            PyValueError::new_err("Unpack failed: MessagePack document breaks a decode bound")
        }
    };
    // The walk's verdict wins over anything the decode raised, as it does when it runs first.
    check_msgpack_structure(data, max_depth).map_err(|what| {
        PyValueError::new_err(format!("Unpack failed: MessagePack document {what}"))
    })?;
    Err(failure)
}
