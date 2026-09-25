"""RedactingSettings: config validation errors never lead back to a raw input (CWE-532).

CachekitConfig and every backend config inherit RedactingSettings, so these run against the concrete
classes users construct, through every entry point: the constructor, from_env() and the
model_validate* classmethods.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import Field, ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from cachekit.backends.base_config import BaseBackendConfig
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.file.config import FileBackendConfig
from cachekit.backends.memcached.config import MemcachedBackendConfig
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.config.settings import CachekitConfig

BACKEND_CONFIGS: list[type[BaseBackendConfig]] = [
    RedisBackendConfig,
    FileBackendConfig,
    CachekitIOBackendConfig,
    MemcachedBackendConfig,
]


def _assert_no_route_to(exc: ValidationError, secret: str) -> None:
    """CWE-532: ``secret`` is absent from every rendering, every input is redacted, the error has no
    chain, and every exception in a ctx has lost its traceback and chain.

    The error's own traceback is not checked: it reaches the caller's frames, which hold what the
    caller passed.
    """
    for rendered in (str(exc), repr(exc), exc.json(), repr(exc.errors())):
        assert secret not in rendered
    assert exc.__context__ is None
    assert exc.__cause__ is None
    for err in exc.errors():
        assert err["input"] == "[REDACTED]"
        for value in (err.get("ctx") or {}).values():
            if isinstance(value, BaseException):
                assert (value.__traceback__, value.__context__, value.__cause__) == (None, None, None)


@dataclasses.dataclass(frozen=True)
class _FrozenError(ValueError):
    """A validator exception whose attributes cannot be set by plain assignment."""

    reason: str


class _FrozenErrorConfig(BaseBackendConfig):
    url: str = ""

    @field_validator("url")
    @classmethod
    def reject(cls, v: str) -> str:
        raise _FrozenError("bad URL")


class _ChainQuotingError(ValueError):
    """A validator exception whose message is rendered from its cause, which quotes the raw value."""

    def __str__(self) -> str:
        return f"bad port: {self.__cause__}"


class _ChainQuotingConfig(BaseBackendConfig):
    port: str = ""
    mapping: dict[str, int] = Field(default_factory=dict)

    @field_validator("port")
    @classmethod
    def reject(cls, v: str) -> str:
        try:
            int(v)
        except ValueError as exc:
            raise _ChainQuotingError() from exc
        return v


class _CustomChainQuotingConfig(BaseBackendConfig):
    port: str = ""

    @field_validator("port")
    @classmethod
    def reject(cls, v: str) -> str:
        try:
            int(v)
        except ValueError as exc:
            quoting = _ChainQuotingError()
            quoting.__cause__ = exc
            raise PydanticCustomError("bad_port", "rejected: {error}", {"error": quoting}) from None
        return v


class _UnrebuildableCtxConfig(BaseBackendConfig):
    """A validator whose custom ctx defeats the rebuild: an object posing as an exception, or a non-str key."""

    url: str = ""
    ctx_kind: str = "proxy"

    @field_validator("url")
    @classmethod
    def reject(cls, v: str, info: ValidationInfo) -> str:
        if info.data.get("ctx_kind") == "proxy":
            raise PydanticCustomError("bad_url", "bad URL: {error}", {"error": MagicMock(spec=ValueError)})
        raise PydanticCustomError("bad_url", "bad URL", {1: "one"})  # type: ignore[dict-item]


@pytest.mark.unit
class TestRedactingSettings:
    """Every surface of a config validation error, for every config class and entry point."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_validation_errors_redact_every_input(self, config_cls: type[BaseBackendConfig]) -> None:
        """CWE-532: backend configs hold credentials, so no surface of a ValidationError may carry a raw input.

        Pinned here, not per backend: the inherited RedactingSettings.__init__ does the redacting, and a
        subclass that overrides __init__ without calling it would silently lose it.
        """
        with pytest.raises(ValidationError) as exc_info:
            config_cls(totally_fake_field_that_doesnt_exist="SECRET_VALUE")  # type: ignore[call-arg]

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize(
        ("env", "build"),
        [
            ({}, lambda: CachekitIOBackendConfig(api_key="ck_live_SECRET_VALUE\n")),  # pragma: allowlist secret
            ({"CACHEKIT_API_KEY": "ck_live_SECRET_VALUE\n"}, CachekitIOBackendConfig.from_env),  # pragma: allowlist secret
            (
                {
                    "CACHEKIT_API_KEY": "ck_live_SECRET_VALUE",  # pragma: allowlist secret
                    "CACHEKIT_API_URL": "https://evil.example.com",
                },
                CachekitIOBackendConfig.from_env,
            ),
            ({"CACHEKIT_PREVIOUS_MASTER_KEYS": "SECRET_VALUE"}, CachekitConfig),
            ({}, lambda: MemcachedBackendConfig(servers=["mc1:SECRET_VALUE"])),
            ({}, lambda: MemcachedBackendConfig(servers=["SECRET_VALUE@mc1"])),
            ({}, lambda: _FrozenErrorConfig(url="SECRET_VALUE")),
        ],
        ids=[
            "io-whitespace-key",
            "io-env-whitespace-key",
            "io-env-allowlist",
            "keyring-env-bad-hex",
            "memcached-port",
            "memcached-format",
            "frozen-validator-exception",
        ],
    )
    def test_validator_errors_leave_no_route_to_the_secret(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], build: Callable[[], object]
    ) -> None:
        """A validator's own exception rides along in ctx["error"]: its message, its chain and its
        traceback's frame locals must not recover the value it rejected. On the from_env() path that
        exception is the only thing still holding the value."""
        monkeypatch.delenv("CACHEKIT_ALLOW_CUSTOM_HOST", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError) as exc_info:
            build()

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize("config_cls", [*BACKEND_CONFIGS, CachekitConfig])
    @pytest.mark.parametrize(
        ("method", "data"),
        [
            ("model_validate", {"totally_fake_field_that_doesnt_exist": "SECRET_VALUE"}),
            ("model_validate", "SECRET_VALUE"),
            ("model_validate", SimpleNamespace(totally_fake_field_that_doesnt_exist="SECRET_VALUE")),
            ("model_validate_json", json.dumps({"totally_fake_field_that_doesnt_exist": "SECRET_VALUE"})),
            ("model_validate_json", '{"api_key": "SECRET_VALUE",'),  # pragma: allowlist secret
            ("model_validate_json", '["SECRET_VALUE"]'),
            ("model_validate_strings", {"totally_fake_field_that_doesnt_exist": "SECRET_VALUE"}),
        ],
        ids=["dict", "non-mapping", "object", "json-object", "json-malformed", "json-array", "strings"],
    )
    def test_model_validate_methods_redact_every_input(
        self, config_cls: type[BaseBackendConfig | CachekitConfig], method: str, data: object
    ) -> None:
        """The inherited validate classmethods build a model too. pydantic calls __init__ only for
        mapping input; malformed JSON, a non-object document, non-mapping input and the strings mode
        fail in the core validator before it."""
        with pytest.raises(ValidationError) as exc_info:
            getattr(config_cls, method)(data)

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_undecodable_env_value_leaves_no_route_to_the_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A list field's env value must be JSON; the decoder's error, chained to pydantic-settings'
        SettingsError, holds the raw value."""
        from pydantic_settings import SettingsError

        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", "user:SECRET_VALUE@mc1:11211")
        with pytest.raises(SettingsError) as exc_info:
            MemcachedBackendConfig.from_env()

        assert "SECRET_VALUE" not in str(exc_info.value)
        assert exc_info.value.__context__ is None
        assert exc_info.value.__cause__ is None

    def test_undecodable_env_file_leaves_no_route_to_the_secret(self, tmp_path: pathlib.Path) -> None:
        """An env file that is not UTF-8 raises UnicodeDecodeError, which carries the whole file's bytes."""
        from pydantic_settings import SettingsError

        env_file = tmp_path / ".env"
        env_file.write_bytes(b"CACHEKIT_MASTER_KEY=SECRET_VALUE\n# caf\xe9\n")

        with pytest.raises(SettingsError) as exc_info:
            CachekitConfig(_env_file=env_file)  # type: ignore[call-arg]
        for rendered in (str(exc_info.value), repr(exc_info.value)):
            assert "SECRET_VALUE" not in rendered
        assert (exc_info.value.__context__, exc_info.value.__cause__) == (None, None)

        with pytest.raises(ValidationError) as validate_info:
            CachekitConfig.model_validate({"_env_file": env_file})
        _assert_no_route_to(validate_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize(
        "build",
        [
            lambda: _ChainQuotingConfig(port="SECRET_VALUE"),
            lambda: _ChainQuotingConfig.model_validate_json('{"port": "SECRET_VALUE"}'),
        ],
        ids=["constructor", "model_validate_json"],
    )
    def test_msg_rendered_from_the_dropped_chain_is_not_restored(self, build: Callable[[], object]) -> None:
        """Dropping a ctx exception's chain changes a msg rendered from it; the rebuild must keep the new msg
        rather than copy the original, which quoted the raw value, back in."""
        with pytest.raises(ValidationError) as exc_info:
            build()

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("value_error", ("port",), "Value error, bad port: None")
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize("ctx_kind", ["proxy", "non-str-key"])
    def test_an_error_the_rebuild_cannot_handle_withholds_its_details(self, ctx_kind: str) -> None:
        """The rebuild runs inside the except; if a validator's ctx makes it raise, that failure would chain the
        original, raw inputs and all. It comes back as one ctx-less error with the details withheld."""
        with pytest.raises(ValidationError) as exc_info:
            _UnrebuildableCtxConfig(ctx_kind=ctx_kind, url="SECRET_VALUE")

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("redaction_failed", (), "Validation failed; details withheld")
        assert "ctx" not in err
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_custom_error_template_is_read_after_the_chain_is_dropped(self) -> None:
        """A custom error rebuilds from its rendered msg; rendered before its ctx exception lost its chain, that
        msg would quote the raw value and become the rebuilt error's template."""
        with pytest.raises(ValidationError) as exc_info:
            _CustomChainQuotingConfig(port="SECRET_VALUE")

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("bad_port", ("port",), "rejected: bad port: None")
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize(
        ("build", "dict_msg"),
        [
            (lambda: _ChainQuotingConfig(port="SECRET_VALUE", mapping="nope"), "Input should be a valid dictionary"),
            (
                lambda: _ChainQuotingConfig.model_validate_json('{"port": "SECRET_VALUE", "mapping": "nope"}'),
                "Input should be an object",
            ),
        ],
        ids=["constructor", "model_validate_json"],
    )
    def test_a_msg_changed_by_the_dropped_chain_leaves_the_others_in_their_mode(
        self, build: Callable[[], object], dict_msg: str
    ) -> None:
        """The rebuild's input mode is chosen by comparing msgs; comparing against a msg as it rendered before
        its chain was dropped makes the Python rebuild look wrong and flips every other error to JSON wording.
        The constructor case pins that; the JSON case is a control (JSON wording either way)."""
        with pytest.raises(ValidationError) as exc_info:
            build()

        by_loc = {err["loc"]: err for err in exc_info.value.errors()}
        assert by_loc[("port",)]["msg"] == "Value error, bad port: None"
        assert (by_loc[("mapping",)]["type"], by_loc[("mapping",)]["msg"]) == ("dict_type", dict_msg)
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize(
        ("config_cls", "doc", "expected"),
        [
            (CachekitIOBackendConfig, '["SECRET_VALUE"]', ("model_type", (), "Input should be an object")),
            (CachekitConfig, '["SECRET_VALUE"]', ("model_type", (), "Input should be an object")),
            (
                MemcachedBackendConfig,
                '{"servers": "SECRET_VALUE"}',
                ("list_type", ("servers",), "Input should be a valid array"),
            ),
        ],
        ids=["io-model-type", "cfg-model-type", "memcached-list-type"],
    )
    def test_json_mode_messages_survive_redaction(
        self, config_cls: type[BaseBackendConfig | CachekitConfig], doc: str, expected: tuple[str, tuple[str, ...], str]
    ) -> None:
        """from_exception_data renders a built-in type's msg in Python input mode by default; a JSON-mode error
        keeps its own msg and, being still a built-in type, its url. The memcached document is an object, so
        __init__ redacts its field error in Python mode first; pydantic re-renders that error in JSON mode, and
        model_validate_json redacts the result."""
        with pytest.raises(ValidationError) as exc_info:
            config_cls.model_validate_json(doc)

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == expected
        assert err["url"].endswith(f"/v/{expected[0]}")
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_non_builtin_error_types_are_redacted_too(self) -> None:
        """A type pydantic-core cannot rebuild by name must still come back as a redacted ValidationError.

        Rebuilding by name raised KeyError inside the except, chaining the raw original. pydantic's own
        Path fields raise "path_type"; a subclass validator may raise PydanticCustomError with a ctx.
        """

        class StrictURLConfig(BaseBackendConfig):
            url: str = ""

            @field_validator("url")
            @classmethod
            def reject(cls, v: str) -> str:
                raise PydanticCustomError("bad_url", "bad URL (port {port})", {"port": 6379})

        with pytest.raises(ValidationError) as file_info:
            FileBackendConfig(cache_dir=None, totally_fake_field_that_doesnt_exist="SECRET_VALUE")  # type: ignore[arg-type,call-arg]
        with pytest.raises(ValidationError) as custom_info:
            StrictURLConfig(url="redis://:SECRET_VALUE@host")

        assert [err["type"] for err in file_info.value.errors()] == ["path_type", "extra_forbidden"]
        [custom] = custom_info.value.errors()
        assert (custom["type"], custom["loc"], custom["msg"], custom["ctx"]) == (
            "bad_url",
            ("url",),
            "bad URL (port 6379)",
            {"port": 6379},
        )
        for exc in (file_info.value, custom_info.value):
            _assert_no_route_to(exc, "SECRET_VALUE")

    def test_unprintable_ctx_exception_reports_nothing_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a ctx exception's str() fails once its chain is dropped, the exception it raises is reported to
        sys.unraisablehook as the msgs are read. Redaction runs outside the except, so no report has the raw
        original as its context."""

        class UnprintableOnceCutError(ValueError):
            def __str__(self) -> str:
                return f"bad port: {self.__cause__.args[0]}"  # pyright: ignore[reportOptionalMemberAccess]

        class PortConfig(BaseBackendConfig):
            port: str = ""

            @field_validator("port")
            @classmethod
            def reject(cls, v: str) -> str:
                try:
                    int(v)
                except ValueError as exc:
                    raise UnprintableOnceCutError() from exc
                return v

        reports: list[BaseException | None] = []
        monkeypatch.setattr(sys, "unraisablehook", lambda unraisable: reports.append(unraisable.exc_value))
        with pytest.raises(ValidationError) as exc_info:
            PortConfig(port="SECRET_VALUE")

        assert reports, "str() no longer fails once the chain is dropped; the test no longer reaches the hook"
        assert all(report is not None and report.__context__ is None for report in reports)
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_custom_error_msg_is_not_formatted_twice(self) -> None:
        """A custom error formats its template with its ctx on every render. A rendered msg that still quotes a
        ctx placeholder must stay literal rather than pull that ctx value into str()."""

        class QuotingConfig(BaseBackendConfig):
            url: str = ""

            @field_validator("url")
            @classmethod
            def reject(cls, v: str) -> str:
                # value before detail: the first render leaves {value} literal, for a second format to fill.
                raise PydanticCustomError("bad_url", "Invalid {detail}", {"value": "SECRET_VALUE", "detail": "{value}"})

        with pytest.raises(ValidationError) as exc_info:
            QuotingConfig(url="x")

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("bad_url", ("url",), "Invalid {value}")
        assert "ctx" not in err
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize("ctx", [None, {"port": 6379}])
    def test_custom_error_named_like_a_builtin_stays_custom(self, ctx: dict[str, int] | None) -> None:
        """A PydanticCustomError named "value_error" is not the built-in: it has no url and no ctx["error"].

        Rebuilding it by name raised TypeError inside the except, chaining the raw original.
        """

        class StrictURLConfig(BaseBackendConfig):
            url: str = ""

            @field_validator("url")
            @classmethod
            def reject(cls, v: str) -> str:
                raise PydanticCustomError("value_error", "rejected (port {port})" if ctx else "rejected", ctx)

        with pytest.raises(ValidationError) as exc_info:
            StrictURLConfig(url="redis://:SECRET_VALUE@host")

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("value_error", ("url",), "rejected (port 6379)" if ctx else "rejected")
        assert "url" not in err
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")
