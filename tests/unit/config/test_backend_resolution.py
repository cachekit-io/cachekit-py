"""Unit tests for backend resolution on the ``@cache(config=...)`` path.

Precedence: ``backend=`` kwarg > ``config.backend`` > the ``set_default_backend()`` default;
the default only fills a config that carries no backend.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cachekit.config.decorator import set_default_backend


@pytest.mark.unit
class TestRoroConfigBackend:
    """@cache(config=...): an explicit config.backend beats set_default_backend().

    Spec rule: an explicit argument MUST override the preset default. The module default
    only fills a config that carries no backend.
    """

    @pytest.fixture(autouse=True)
    def _resolved(self, monkeypatch: pytest.MonkeyPatch) -> list:
        """Capture the DecoratorConfig the decorator resolves, instead of building a wrapper."""
        seen: list = []

        def spy(f, config, **_kwargs):
            seen.append(config)
            return f

        monkeypatch.setattr("cachekit.decorators.intent._apply_cache_logic", spy)
        yield seen
        set_default_backend(None)

    @staticmethod
    def _decorate(**kwargs) -> None:
        from cachekit import cache

        @cache(**kwargs)
        def fn() -> int:
            return 1

    def test_config_backend_beats_default_with_other_overrides(self, _resolved: list) -> None:
        from cachekit.config.decorator import DecoratorConfig

        explicit, default = MagicMock(), MagicMock()
        set_default_backend(default)
        self._decorate(config=DecoratorConfig.production(backend=explicit), ttl=5)
        assert _resolved[0].backend is explicit
        assert _resolved[0].ttl == 5

    @pytest.mark.parametrize("default", [MagicMock(), None], ids=["default-set", "no-default"])
    def test_default_fills_a_config_without_backend(self, _resolved: list, default: object) -> None:
        """No default leaves None at decoration; the wrapper resolves it at first call."""
        from cachekit.config.decorator import DecoratorConfig

        set_default_backend(default)  # type: ignore[arg-type]
        self._decorate(config=DecoratorConfig.production())
        assert _resolved[0].backend is default

    def test_explicit_kwarg_still_beats_config_backend(self, _resolved: list) -> None:
        from cachekit.config.decorator import DecoratorConfig

        in_config, kwarg = MagicMock(), MagicMock()
        self._decorate(config=DecoratorConfig.production(backend=in_config), backend=kwarg)
        assert _resolved[0].backend is kwarg

    def test_io_config_keeps_its_own_backend_under_a_default(self, _resolved: list) -> None:
        """The cross-tenant case: io(api_key=B) under a key-A default must stay on key B."""
        from cachekit.config.decorator import DecoratorConfig

        set_default_backend(MagicMock())  # tenant A's backend
        io = DecoratorConfig.io(api_key="ck_live_TENANT_B")  # pragma: allowlist secret
        self._decorate(config=io)
        assert _resolved[0].backend is io.backend
