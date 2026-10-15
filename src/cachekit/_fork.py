"""Whether this process first imported cachekit in the child of a fork made from C whose parent ran threads.

Such a fork, as uWSGI's is without ``--py-call-uwsgi-fork-hooks``, runs no at-fork hook, CPython's own after-fork
repair included, so a thread started in the child can hang in ``Thread.start()`` or crash the interpreter. A
process that imported cachekit before the fork sees its PID change; one that first imports it after the fork
(uWSGI ``--lazy-apps``) has no PID to compare with. ``l1_cache`` and ``async_metrics`` take ``IMPORT_PID`` as the
PID that imported them, so such a process counts as hookless there, and every gate that reads them applies.

Stdlib only, and imported first by ``cachekit``: the check runs once, before cachekit starts anything.
"""

import os
import threading
from typing import Optional


def inherits_dead_threads() -> bool:
    """Whether ``threading`` lists a thread this process does not have, which only a fork made from C leaves.

    An ``os.fork()`` child runs ``threading``'s after-fork hook, which drops the parent's threads. A fork made
    from C runs none, so the parent's threads stay listed under native ids that are no task of this process.
    So this is False in a fresh process, live threads or not, and in the child of a parent that ran no Python
    threads, where a thread start is safe.

    Neither the current thread nor the main thread counts, nor any thread listed under either's native id.
    After any fork, the forking thread's object may keep the parent's native id: always after a fork made
    from C, and after ``os.fork()`` on Python before 3.13. It is the main thread after ``os.fork()``, and after
    a fork made from C from the parent's main thread, as uWSGI's is; it is the current thread when cachekit is
    imported on it. A thread under the same id, such as a gevent green thread, ran on that thread and not on
    one of its own. So a fork made from C from a thread other than the main one loses the parent's main thread
    as evidence: only its other threads count. Nor does a ``_DummyThread``, which ``threading`` may keep listed after its
    thread ends, nor a thread that has no native id yet. Threads ``threading`` never registered
    (``_thread.start_new_thread``, foreign C threads) are not seen. Linux only: with no ``/proc/self/task``,
    or with this thread's own id absent from it (another PID namespace, or a patched ``get_native_id``), it
    cannot tell and returns False, as if fresh.

    Other threads may start and end while this runs. Each thread's task is looked up by its id, never read off a
    listing of ``/proc/self/task``, which can skip a live task while threads come and go. And a thread counts only if
    ``threading`` still lists it after that lookup: a thread leaves the list before its task ends.
    """
    if not _has_task(threading.get_native_id()):
        return False
    forkers = {threading.current_thread().native_id, threading.main_thread().native_id}
    gone = [
        thread
        for thread in threading.enumerate()
        if thread.native_id is not None
        and thread.native_id not in forkers
        and not isinstance(thread, threading._DummyThread)
        and not _has_task(thread.native_id)
    ]
    if not gone:
        return False
    listed = {id(thread) for thread in threading.enumerate()}  # identity: gone keeps each object alive
    return any(id(thread) in listed for thread in gone)


def _has_task(native_id: int) -> bool:
    """Whether this process has a thread with this native id; False also when /proc cannot say."""
    return os.path.exists(f"/proc/self/task/{native_id}")


# The PID that imported cachekit, or None in a process that inherited a threaded parent's dead threads: no PID
# of such a process is safe for a thread. An os.fork() child of it runs the at-fork hooks, so it is safe again.
IMPORT_PID: Optional[int] = None if inherits_dead_threads() else os.getpid()
