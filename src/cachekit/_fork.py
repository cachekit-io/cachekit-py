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

    The current thread does not count: after a fork made from C it keeps the forking thread's native id. Nor
    does any thread listed under that same id, such as a gevent green thread, which ran on the forking thread
    and not on a thread of its own. Nor does a ``_DummyThread``, which ``threading`` may keep listed after its
    thread ends, nor a thread that has no native id yet. Threads ``threading`` never registered (``_thread.start_new_thread``, foreign C threads)
    are not seen. Linux only: with no ``/proc/self/task``, or with this thread's own id absent from it
    (another PID namespace, or a patched ``get_native_id``), it cannot tell and returns
    False, as if fresh.
    """
    try:
        tasks = {int(task) for task in os.listdir("/proc/self/task")}
    except (OSError, ValueError):
        return False
    if threading.get_native_id() not in tasks:
        return False
    current = threading.current_thread()
    return any(
        thread is not current
        and thread.native_id is not None
        and thread.native_id != current.native_id
        and not isinstance(thread, threading._DummyThread)
        and thread.native_id not in tasks
        for thread in threading.enumerate()
    )


# The PID that imported cachekit, or None in a process that inherited a threaded parent's dead threads: no PID
# of such a process is safe for a thread. An os.fork() child of it runs the at-fork hooks, so it is safe again.
IMPORT_PID: Optional[int] = None if inherits_dead_threads() else os.getpid()
