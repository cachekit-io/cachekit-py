//! The decorator's memoised L1-only hit, answered by one call that takes no lock.
//!
//! `@cache(backend=None)` keeps its values in an `ObjectCache` (`src/cachekit/object_cache.py`), whose
//! writes and removals run under its lock. A hit does not take it: [`HitMemo::get`] maps the call's
//! arguments ([`memo_key`]) to the [`HitEntry`] the cache stored for them, in one dict lookup, and
//! serves the entry's value while the entry is alive and before its deadline, counting the hit on the
//! function's `HitCounter`.
//!
//! The cache keeps the memo in step under its lock. An entry it stores or finds for a call is memoised
//! under that call's key, and an entry it removes (expiry, eviction, invalidation, clear, the at-fork
//! reset) is killed and forgotten before the removal returns. So a hit that starts after a removal has
//! returned never serves the removed value: it finds no memo slot, or it finds the entry dead. And no
//! removed value stays referenced from here, as the memo holds no removed entry.
//!
//! Deadlines are read on one clock, [`monotonic`], which `ObjectCache` uses too.

use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

use pyo3::ffi;
use pyo3::gc::PyVisit;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use pyo3::PyTraverseError;

use crate::hit_counter::PyHitCounter;

/// A call's str and bytes arguments and keyword names may hold at most this many characters or bytes
/// between them: the memo keeps what it is keyed by. Mirrors `_MEMO_MAX_LEN` in decorators/wrapper.py.
const MEMO_MAX_LEN: ffi::Py_ssize_t = 256;
/// Arguments per call, positional and keyword together (`_MEMO_MAX_ARGS`).
const MEMO_MAX_ARGS: usize = 8;
/// Memo slots per decorated function (`_MEMO_MAX_ENTRIES`). A full memo is emptied rather than
/// evicted slot by slot, so a hit writes nothing to it.
const MEMO_MAX_ENTRIES: usize = 1024;

// ── The clock ────────────────────────────────────────────────────────────────────────────────

/// The clock's zero: when the module first read it. `Instant` is the OS monotonic clock
/// (`CLOCK_MONOTONIC` on Linux, the same clock `time.monotonic()` reads there), and a forked child
/// inherits the zero with the rest of its memory, so readings stay comparable across a fork.
static EPOCH: OnceLock<Instant> = OnceLock::new();

/// A test's replacement clock, a Python callable returning seconds, or null for the real one.
/// Installed only by tests, through `_set_clock`. A replaced callable is never released, so a
/// reader that loaded the pointer before a replacement still holds a live object.
static CLOCK_OVERRIDE: AtomicPtr<ffi::PyObject> = AtomicPtr::new(ptr::null_mut());

fn now(py: Python<'_>) -> PyResult<f64> {
    let fake = CLOCK_OVERRIDE.load(Ordering::Acquire);
    if fake.is_null() {
        return Ok(EPOCH.get_or_init(Instant::now).elapsed().as_secs_f64());
    }
    // SAFETY: a non-null override is a strong reference this module owns and never releases.
    unsafe { Bound::from_borrowed_ptr(py, fake) }
        .call0()?
        .extract()
}

/// Seconds on the clock `ObjectCache` and the lock-free hit share, from an arbitrary zero.
///
/// One clock for both, so an entry's deadline, computed by the cache, means the same instant to
/// the hit that compares against it.
#[pyfunction]
pub fn monotonic(py: Python<'_>) -> PyResult<f64> {
    now(py)
}

/// Replace the shared clock with a callable returning seconds, or restore the real one with None.
///
/// For tests only: the real clock cannot be moved, and a test of expiry or refresh needs both the
/// cache and the lock-free hit to see the same moved time. Each replaced callable is kept alive for
/// the rest of the process.
#[pyfunction]
#[pyo3(name = "_set_clock")]
pub fn set_clock(clock: Option<Bound<'_, PyAny>>) {
    let new = clock.map_or(ptr::null_mut(), Bound::into_ptr);
    CLOCK_OVERRIDE.store(new, Ordering::Release);
}

// ── The memo key ─────────────────────────────────────────────────────────────────────────────

/// The `i`th item of a tuple, borrowed: the item macro where the build has the tuple's layout, a
/// call to `PyTuple_GetItem` on the stable ABI.
///
/// # Safety
/// `i` must be in bounds.
#[inline]
unsafe fn tuple_item(t: &Bound<'_, PyTuple>, i: usize) -> *mut ffi::PyObject {
    t.get_borrowed_item_unchecked(i).as_ptr()
}

/// What an argument spends of its call's [`MEMO_MAX_LEN`], and whether its type must be part of
/// the key; None for anything the memo never takes.
///
/// Types are tested by identity, so an argument's type is never hashed or compared through its
/// metaclass, and a subclass (an `IntEnum`, a `str` subclass) or a container never matches. An int,
/// str or None never compares equal to a value of another of those types, so a call of only those
/// can be keyed by its argument tuple as it is. A float or bool can (1, 1.0 and True are equal), and
/// a bytes compared with an equal str warns under `python -b`, so their types go in the key.
///
/// # Safety
/// `a` must be a valid object pointer.
#[inline]
unsafe fn admit(a: *mut ffi::PyObject) -> Option<(ffi::Py_ssize_t, bool)> {
    let t = ffi::Py_TYPE(a);
    if t == &raw mut ffi::PyLong_Type || a == ffi::Py_None() {
        Some((0, false))
    } else if t == &raw mut ffi::PyUnicode_Type {
        Some((ffi::PyUnicode_GetLength(a), false))
    } else if t == &raw mut ffi::PyFloat_Type || t == &raw mut ffi::PyBool_Type {
        Some((0, true))
    } else if t == &raw mut ffi::PyBytes_Type {
        Some((ffi::PyBytes_Size(a), true))
    } else {
        None
    }
}

/// An object's type, as a new reference.
///
/// # Safety
/// `a` must be a valid object pointer.
unsafe fn type_of<'py>(py: Python<'py>, a: *mut ffi::PyObject) -> Bound<'py, PyAny> {
    Bound::from_borrowed_ptr(py, ffi::Py_TYPE(a).cast())
}

/// The memo key of a call's arguments, or None when the memo does not take the call.
///
/// The admission rules (`_memo_key` in decorators/wrapper.py documents them): every value an exact
/// int, float, bool, None, str or bytes; every keyword name an exact str; at most
/// [`MEMO_MAX_ARGS`] arguments; at most [`MEMO_MAX_LEN`] characters or bytes in the str and bytes
/// values and the keyword names together. The shapes: the argument tuple itself when [`admit`]
/// needs no types; `(t, a)` and `(t, a, u, b)` for one and two typed positional arguments;
/// `(args, types)` for more; `(args, types, items, item_types)` with keywords, sorted by name so
/// keywords in any order share one slot. No two shapes are equal: an untyped key holds no type
/// and no tuple, the two- and four-item typed keys start with a type, the others with a tuple.
fn memo_key_of<'py>(
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = args.py();
    let n = args.len();
    let nk = kwargs.len();
    if n + nk > MEMO_MAX_ARGS {
        return Ok(None);
    }
    let mut size: ffi::Py_ssize_t = 0;
    let mut typed = false;
    for i in 0..n {
        // SAFETY: i < n = len(args), and args holds valid objects.
        match unsafe { admit(tuple_item(args, i)) } {
            Some((cost, needs_type)) => {
                size += cost;
                typed |= needs_type;
            }
            None => return Ok(None),
        }
    }
    if nk == 0 {
        if size > MEMO_MAX_LEN {
            return Ok(None);
        }
        if !typed {
            return Ok(Some(args.clone().into_any()));
        }
        // SAFETY: as above.
        unsafe {
            if n == 1 {
                let a = tuple_item(args, 0);
                return Ok(Some(
                    PyTuple::new(py, [type_of(py, a), Bound::from_borrowed_ptr(py, a)])?.into_any(),
                ));
            }
            if n == 2 {
                let (a, b) = (tuple_item(args, 0), tuple_item(args, 1));
                let items = [
                    type_of(py, a),
                    Bound::from_borrowed_ptr(py, a),
                    type_of(py, b),
                    Bound::from_borrowed_ptr(py, b),
                ];
                return Ok(Some(PyTuple::new(py, items)?.into_any()));
            }
            let types = PyTuple::new(py, (0..n).map(|i| type_of(py, tuple_item(args, i))))?;
            return Ok(Some(
                PyTuple::new(py, [args.as_any(), types.as_any()])?.into_any(),
            ));
        }
    }
    let mut items: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = Vec::with_capacity(nk);
    for (name, value) in kwargs.iter() {
        // SAFETY: both are valid objects. A str subclass name equals its str and so would share the
        // slot, but the key generator refuses it, so it must take the full path.
        let Some((cost, _)) = (unsafe { admit(value.as_ptr()) }) else {
            return Ok(None);
        };
        if unsafe { ffi::Py_TYPE(name.as_ptr()) } != &raw mut ffi::PyUnicode_Type {
            return Ok(None);
        }
        size += unsafe { ffi::PyUnicode_GetLength(name.as_ptr()) } + cost;
        items.push((name, value));
    }
    if size > MEMO_MAX_LEN {
        return Ok(None);
    }
    // Names are distinct exact str: comparing them never compares a value or runs Python code.
    let mut failed = None;
    items.sort_by(|x, y| {
        x.0.compare(&y.0).unwrap_or_else(|e| {
            failed.get_or_insert(e);
            std::cmp::Ordering::Equal
        })
    });
    if let Some(e) = failed {
        return Err(e);
    }
    // SAFETY: args holds valid objects, and i < n.
    let types = PyTuple::new(
        py,
        (0..n).map(|i| unsafe { type_of(py, tuple_item(args, i)) }),
    )?;
    let item_types = PyTuple::new(py, items.iter().map(|(_, v)| v.get_type().into_any()))?;
    let pairs = PyTuple::new(
        py,
        items
            .iter()
            .map(|(k, v)| PyTuple::new(py, [k, v]).map(Bound::into_any))
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(Some(
        PyTuple::new(
            py,
            [
                args.as_any(),
                types.as_any(),
                pairs.as_any(),
                item_types.as_any(),
            ],
        )?
        .into_any(),
    ))
}

/// The memo key of a call's arguments, or None when the memo does not take the call.
///
/// The key the lock-free hit looks up, for the decorator's slow path to memoise its entry under.
#[pyfunction]
pub fn memo_key<'py>(
    args: &Bound<'py, PyTuple>,
    kwargs: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    memo_key_of(args, kwargs)
}

// ── Entries and the memo ─────────────────────────────────────────────────────────────────────

/// What a lock-free hit needs of one `ObjectCache` entry: its value, the time before which it may be
/// served without the cache's lock, whether it is still stored, and when such a hit last read it.
///
/// The value and deadline never change: the cache stores a new `HitEntry` when it refreshes an entry.
/// The cache kills an entry under its lock when it removes it, and a hit reads `alive` after it
/// found the entry, with acquire ordering, so a removal that has returned before a hit starts is seen.
#[pyclass(frozen, module = "cachekit._rust_serializer")]
pub struct HitEntry {
    value: Py<PyAny>,
    fresh_until: f64,
    alive: AtomicBool,
    /// f64 bits of the clock reading of the latest lock-free hit: the cache's LRU order reads it.
    used_at: AtomicU64,
}

#[pymethods]
impl HitEntry {
    #[new]
    fn new(value: Py<PyAny>, fresh_until: f64, used_at: f64) -> Self {
        Self {
            value,
            fresh_until,
            alive: AtomicBool::new(true),
            used_at: AtomicU64::new(used_at.to_bits()),
        }
    }

    /// End the entry for lock-free hits; the cache calls this, under its lock, on every removal.
    fn kill(&self) {
        self.alive.store(false, Ordering::Release);
    }

    /// Whether the entry is still stored.
    #[getter]
    fn alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }

    /// The clock reading of the latest lock-free hit, or the one the entry was created with.
    #[getter]
    fn used_at(&self) -> f64 {
        f64::from_bits(self.used_at.load(Ordering::Relaxed))
    }

    /// The time before which a lock-free hit may serve the value.
    #[getter]
    fn fresh_until(&self) -> f64 {
        self.fresh_until
    }

    /// The stored object, as a hit returns it.
    #[getter]
    fn value(&self, py: Python<'_>) -> Py<PyAny> {
        self.value.clone_ref(py)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.value)
    }
}

/// One decorated function's memo: memo key -> the `HitEntry` the function's `ObjectCache` holds for
/// that call, and the lock-free hit that reads it.
///
/// Only the cache writes the memo, under its lock (`set`, `forget`, `clear`); `get` only reads it,
/// which CPython's dict allows from any thread. A memo answers only while the function's
/// `__module__` and `__qualname__` are the objects it was built with, which its cache keys embed:
/// renaming the function re-keys its calls, as the key generator reads the names on every call.
#[pyclass(frozen, module = "cachekit._rust_serializer")]
pub struct HitMemo {
    memo: Py<PyDict>,
    counter: Py<PyHitCounter>,
    func: Py<PyAny>,
    module: Py<PyAny>,
    qualname: Py<PyAny>,
}

impl HitMemo {
    /// Whether the function still has the names the memo was built with (an attribute that cannot
    /// be read counts as changed).
    fn names_hold(&self, py: Python<'_>) -> bool {
        let f = self.func.bind(py);
        let same = |name: &Bound<'_, pyo3::types::PyString>, held: &Py<PyAny>| {
            f.getattr(name).is_ok_and(|v| v.is(held))
        };
        same(intern!(py, "__module__"), &self.module)
            && same(intern!(py, "__qualname__"), &self.qualname)
    }
}

#[pymethods]
impl HitMemo {
    /// A memo for `func`, counting its hits on `counter`, answering while `func` has these names.
    #[new]
    fn new(
        py: Python<'_>,
        func: Py<PyAny>,
        counter: Py<PyHitCounter>,
        module: Py<PyAny>,
        qualname: Py<PyAny>,
    ) -> Self {
        Self {
            memo: PyDict::new(py).unbind(),
            counter,
            func,
            module,
            qualname,
        }
    }

    /// The call's cached value, or `miss` when this memo cannot answer for it.
    ///
    /// Answers when the call's memo key has a slot, its entry is alive and before its deadline, and
    /// the function has its names; then it stamps the entry's LRU recency and counts the hit. Any
    /// other call, a stale or expired one included, gets `miss` and goes to the cache's lock.
    fn get(
        &self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: &Bound<'_, PyDict>,
        miss: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let Some(key) = memo_key_of(args, kwargs)? else {
            return Ok(miss);
        };
        let Some(found) = self.memo.bind(py).get_item(&key)? else {
            return Ok(miss);
        };
        // SAFETY: set() stores HitEntry objects only.
        let entry = unsafe { found.cast_unchecked::<HitEntry>() }.get();
        if !entry.alive.load(Ordering::Acquire) {
            return Ok(miss);
        }
        let now = now(py)?;
        // `<`, not a negated `>=`: a NaN on either side is a miss
        if now < entry.fresh_until && self.names_hold(py) {
            entry.used_at.store(now.to_bits(), Ordering::Relaxed);
            self.counter.get().hit();
            return Ok(entry.value.clone_ref(py));
        }
        Ok(miss)
    }

    /// Point `key`'s slot at `entry`. Call under the cache's lock.
    fn set(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        entry: &Bound<'_, HitEntry>,
    ) -> PyResult<()> {
        let memo = self.memo.bind(py);
        if memo.len() >= MEMO_MAX_ENTRIES && !memo.contains(key)? {
            memo.clear();
        }
        memo.set_item(key, entry)
    }

    /// Drop `key`'s slot if it holds `entry`. Call under the cache's lock.
    fn forget(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        entry: &Bound<'_, HitEntry>,
    ) -> PyResult<()> {
        let memo = self.memo.bind(py);
        if let Some(held) = memo.get_item(key)? {
            if held.is(entry) {
                memo.del_item(key)?;
            }
        }
        Ok(())
    }

    /// Drop every slot. Call under the cache's lock.
    fn clear(&self, py: Python<'_>) {
        self.memo.bind(py).clear();
    }

    /// The memo keys with a slot, for tests.
    fn keys<'py>(&self, py: Python<'py>) -> Bound<'py, pyo3::types::PyList> {
        self.memo.bind(py).keys()
    }

    fn __len__(&self, py: Python<'_>) -> usize {
        self.memo.bind(py).len()
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.memo)?;
        visit.call(&self.counter)?;
        visit.call(&self.func)?;
        visit.call(&self.module)?;
        visit.call(&self.qualname)
    }
}
