"""create_cache_wrapper must reject keyword arguments it does not declare.

A ``**kwargs`` catch-all here silently dropped misspelled or removed options
(e.g. the ``_``-prefixed reliability tuning keys), returning a working wrapper
that ignored them.
"""

from __future__ import annotations

import pytest

from cachekit.decorators.wrapper import create_cache_wrapper


@pytest.mark.unit
def test_unknown_keyword_argument_raises_type_error() -> None:
    def fn() -> int:
        return 1

    with pytest.raises(TypeError, match="_health_check_level"):
        create_cache_wrapper(fn, _health_check_level="full")  # type: ignore[call-arg]
