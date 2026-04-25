"""Unit tests for @cache.local() decorator via the public cache API.

All tests are isolated; no Redis or external services required.
"""

from __future__ import annotations

import pytest

from cachekit import cache


@pytest.mark.unit
class TestLocalWrapperRejectedParams:
    """Parameters that @cache.local() must explicitly reject."""

    def test_backend_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"@cache\.local\(\)"):

            @cache.local(backend=object())
            def fn() -> None:
                pass

    def test_serializer_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"@cache\.local\(\)"):

            @cache.local(serializer="std")
            def fn() -> None:
                pass

    def test_encryption_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"@cache\.local\(\)"):

            @cache.local(encryption=True)
            def fn() -> None:
                pass

    def test_master_key_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"@cache\.local\(\)"):

            @cache.local(master_key="abc")
            def fn() -> None:
                pass

    def test_integrity_checking_rejected(self) -> None:
        with pytest.raises(TypeError, match=r"@cache\.local\(\)"):

            @cache.local(integrity_checking=True)
            def fn() -> None:
                pass

    def test_config_rejected(self) -> None:
        """config= is intercepted in intent.py before local_wrapper; must still raise TypeError."""
        with pytest.raises(TypeError):

            @cache.local(config=object())
            def fn() -> None:
                pass


@pytest.mark.unit
class TestLocalWrapperValidation:
    """Value validation for accepted parameters."""

    def test_ttl_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="ttl"):

            @cache.local(ttl=0)
            def fn() -> None:
                pass

    def test_ttl_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="ttl"):

            @cache.local(ttl=-5)
            def fn() -> None:
                pass

    def test_max_entries_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):

            @cache.local(max_entries=0)
            def fn() -> None:
                pass


@pytest.mark.unit
class TestLocalWrapperCustomKey:
    """Custom key= callable collapses distinct args to a single cache entry."""

    def test_fixed_key_collapses_calls(self) -> None:
        call_count = 0

        @cache.local(key=lambda *a, **kw: "fixed")
        def fn(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 10

        r1 = fn(1)
        r2 = fn(999)  # Different arg, same key — must be a cache hit

        assert r1 == 10
        assert r2 == 10  # Returns the first cached result
        assert call_count == 1


@pytest.mark.unit
class TestLocalWrapperAsync:
    """Async-function-specific wrapper API."""

    async def test_ainvalidate_cache_works(self) -> None:
        call_count = 0

        @cache.local()
        async def afn(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await afn(5)  # miss — computes and caches
        await afn(5)  # hit
        assert call_count == 1

        await afn.ainvalidate_cache(5)  # evict

        await afn(5)  # miss again — re-computed
        assert call_count == 2

    async def test_cache_clear_on_async_does_not_raise(self) -> None:
        """cache_clear() on an async-wrapped function must not raise TypeError and must clear state."""

        @cache.local()
        async def afn(x: int) -> int:
            return x

        await afn(1)  # populate
        assert afn.cache_info().currsize == 1

        afn.cache_clear()  # must not raise
        assert afn.cache_info().currsize == 0


@pytest.mark.unit
class TestLocalWrapperMutation:
    """@cache.local() stores object references — mutations affect cached value."""

    def test_mutating_returned_dict_affects_cache(self) -> None:
        @cache.local()
        def get_data() -> dict[str, int]:
            return {"count": 0}

        first = get_data()
        first["count"] += 1

        second = get_data()
        # Reference semantics: second is the same object
        assert second["count"] == 1
        assert first is second


@pytest.mark.unit
class TestLocalWrapperKeyGeneration:
    """Unhashable argument types (lists, dicts) must not raise TypeError."""

    def test_list_arg_does_not_raise(self) -> None:
        @cache.local()
        def process(items: list[int]) -> int:
            return sum(items)

        # Must not raise; key generator handles lists via JSON normalisation
        result = process([1, 2, 3])
        assert result == 6

        # Second call with same list — should be a cache hit (same result, no error)
        result2 = process([1, 2, 3])
        assert result2 == 6

    def test_dict_kwarg_does_not_raise(self) -> None:
        @cache.local()
        def process(opts: dict[str, int]) -> int:
            return len(opts)

        result = process({"a": 1, "b": 2})
        assert result == 2

        # Second call — cache hit, no error
        result2 = process({"a": 1, "b": 2})
        assert result2 == 2
