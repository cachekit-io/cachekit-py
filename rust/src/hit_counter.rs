//! Lock-free L1-hit counter for the decorator's per-function statistics.
//!
//! The one piece of this crate that is not a wrapper around cachekit-core: Python has no
//! atomic integer, and `HitCounter.hit()` runs once per decorated L1 hit, the decorator's
//! hottest path, where taking a Python lock cost more than the rest of the hit's bookkeeping.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(feature = "python")]
use pyo3::prelude::*;

const NANOS_PER_SEC: u64 = 1_000_000_000;

/// Counts hits and stamps the latest one with the wall-clock time, without a lock.
///
/// Each field is exact on its own. A reader that loads both can see a hit that is in one and
/// not yet in the other; the decorator's statistics accept that, as they are not a snapshot
/// of one instant.
#[derive(Default)]
pub struct HitCounter {
    count: AtomicU64,
    /// Nanoseconds since the Unix epoch of the latest hit; 0 means none since the last reset.
    last_hit_ns: AtomicU64,
}

impl HitCounter {
    /// Count one hit and stamp it with the current wall-clock time.
    pub fn hit(&self) {
        self.count.fetch_add(1, Ordering::Relaxed);
        // fetch_max, not store: of two racing hits, the later stamp stays whichever lands last.
        self.last_hit_ns.fetch_max(now_ns(), Ordering::Relaxed);
    }

    /// Zero the count and forget the latest hit.
    pub fn reset(&self) {
        self.count.store(0, Ordering::Relaxed);
        self.last_hit_ns.store(0, Ordering::Relaxed);
    }

    /// Hits counted since construction or the last reset.
    pub fn count(&self) -> u64 {
        self.count.load(Ordering::Relaxed)
    }

    /// The latest hit's time in seconds since the Unix epoch, as `time.time()` would have
    /// returned it then, or None when there has been none since the last reset.
    pub fn last_hit(&self) -> Option<f64> {
        match self.last_hit_ns.load(Ordering::Relaxed) {
            0 => None,
            ns => Some(ns_to_seconds(ns)),
        }
    }
}

/// The wall clock (`CLOCK_REALTIME` on Linux, as `time.time()` reads), in nanoseconds.
fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX))
}

/// Seconds as CPython's `PyTime_AsSecondsDouble` computes them from nanoseconds, so a stamp
/// compares with a `time.time()` reading taken just before or after it in the right order.
#[allow(clippy::cast_precision_loss)]
fn ns_to_seconds(ns: u64) -> f64 {
    if ns % NANOS_PER_SEC == 0 {
        (ns / NANOS_PER_SEC) as f64
    } else {
        ns as f64 / 1e9
    }
}

/// Python wrapper for `HitCounter`: `cachekit._rust_serializer.HitCounter`.
#[cfg(feature = "python")]
#[pyclass(name = "HitCounter", frozen, module = "cachekit._rust_serializer")]
#[derive(Default)]
pub struct PyHitCounter {
    inner: HitCounter,
}

#[cfg(feature = "python")]
#[pymethods]
impl PyHitCounter {
    #[new]
    pub fn new() -> Self {
        Self::default()
    }

    /// Count one hit and stamp it with the current wall-clock time.
    pub fn hit(&self) {
        self.inner.hit();
    }

    /// Zero the count and forget the latest hit.
    pub fn reset(&self) {
        self.inner.reset();
    }

    /// Hits counted since construction or the last reset.
    #[getter]
    pub fn count(&self) -> u64 {
        self.inner.count()
    }

    /// The latest hit's time in seconds since the Unix epoch, or None.
    #[getter]
    pub fn last_hit(&self) -> Option<f64> {
        self.inner.last_hit()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_and_resets() {
        let counter = HitCounter::default();
        assert_eq!(counter.count(), 0);
        assert!(counter.last_hit().is_none());
        counter.hit();
        counter.hit();
        assert_eq!(counter.count(), 2);
        assert!(counter.last_hit().is_some());
        counter.reset();
        assert_eq!(counter.count(), 0);
        assert!(counter.last_hit().is_none());
    }

    #[test]
    fn concurrent_hits_are_all_counted() {
        let counter = HitCounter::default();
        std::thread::scope(|scope| {
            for _ in 0..8 {
                scope.spawn(|| {
                    for _ in 0..10_000 {
                        counter.hit();
                    }
                });
            }
        });
        assert_eq!(counter.count(), 80_000);
    }

    #[test]
    fn whole_seconds_convert_exactly() {
        assert_eq!(
            ns_to_seconds(1_700_000_000 * NANOS_PER_SEC),
            1_700_000_000.0
        );
        assert_eq!(ns_to_seconds(1_500_000_000), 1.5);
    }
}
