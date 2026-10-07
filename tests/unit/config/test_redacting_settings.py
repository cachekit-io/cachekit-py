"""RedactingSettings: config validation errors never lead back to a raw input (CWE-532).

CachekitConfig and every backend config inherit RedactingSettings, so these run against the concrete
classes users construct, through each entry point: the constructor, from_env(), the
model_validate* classmethods, a pydantic.TypeAdapter and a field of the caller's own model.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import pathlib
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, ValidationInfo, create_model, field_validator
from pydantic_core import PydanticCustomError
from urllib3.exceptions import LocationParseError

import cachekit
from cachekit import DecoratorConfig, cache
from cachekit._rust_serializer import KeyringConfigurationError
from cachekit.backends.base_config import BaseBackendConfig
from cachekit.backends.cachekitio import CachekitIOBackend
from cachekit.backends.cachekitio import client as cachekitio_client
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.file.config import FileBackendConfig
from cachekit.backends.memcached.config import MemcachedBackendConfig
from cachekit.backends.redis import RedisBackend
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config import ConfigurationError, singleton, validate_encryption_config
from cachekit.config.nested import EncryptionConfig
from cachekit.config.settings import CachekitConfig
from cachekit.config.validation import _BYTES_KEY_REFUSAL
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper

_KEY_HEX = "ab" * 32

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

    ctx_kind: str = "proxy"  # before url, so url's validator can read it
    url: str = ""

    @field_validator("url")
    @classmethod
    def reject(cls, v: str, info: ValidationInfo) -> str:
        if info.data["ctx_kind"] == "proxy":  # indexed: a missing ctx_kind is a KeyError, not the other branch
            raise PydanticCustomError("bad_url", "bad URL: {error}", {"error": MagicMock(spec=ValueError)})
        raise PydanticCustomError("bad_url", "bad URL", {1: "one"})  # type: ignore[dict-item]


class _SelfReferentialConfig(RedisBackendConfig):
    child: _SelfReferentialConfig | None = None


@pytest.mark.unit
class TestRedactingSettings:
    """Each surface of a config validation error, for each config class and entry point."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_validation_errors_redact_the_input(self, config_cls: type[BaseBackendConfig]) -> None:
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
    def test_model_validate_methods_redact_the_input(
        self, config_cls: type[BaseBackendConfig | CachekitConfig], method: str, data: object
    ) -> None:
        """The inherited validate classmethods build a model too. pydantic calls __init__ only for
        mapping input; malformed JSON, a non-object document, non-mapping input and the strings mode
        fail in the core validator before it."""
        with pytest.raises(ValidationError) as exc_info:
            getattr(config_cls, method)(data)

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize("config_cls", [*BACKEND_CONFIGS, CachekitConfig])
    @pytest.mark.parametrize(
        ("method", "data"),
        [
            ("validate_python", "SECRET_VALUE"),
            ("validate_python", ["SECRET_VALUE"]),
            ("validate_json", '"SECRET_VALUE"'),
            ("validate_json", '["SECRET_VALUE"]'),
            ("validate_strings", {"totally_fake_field_that_doesnt_exist": "SECRET_VALUE"}),
        ],
        ids=["non-mapping", "list", "json-string", "json-array", "strings"],
    )
    def test_type_adapter_redacts_the_input(
        self, config_cls: type[BaseBackendConfig | CachekitConfig], method: str, data: object
    ) -> None:
        """A TypeAdapter validates through the core schema and never calls the model_validate* classmethods."""
        with pytest.raises(ValidationError) as exc_info:
            getattr(TypeAdapter(config_cls), method)(data)

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize("config_cls", [*BACKEND_CONFIGS, CachekitConfig])
    @pytest.mark.parametrize(
        ("method", "data"),
        [
            ("__call__", {"cfg": "SECRET_VALUE"}),
            ("model_validate", {"cfg": ["SECRET_VALUE"]}),
            ("model_validate_json", '{"cfg": "SECRET_VALUE"}'),
            ("model_validate_strings", {"cfg": {"totally_fake_field_that_doesnt_exist": "SECRET_VALUE"}}),
        ],
        ids=["constructor", "model_validate", "json", "strings"],
    )
    def test_config_as_a_field_redacts_the_input(
        self, config_cls: type[BaseBackendConfig | CachekitConfig], method: str, data: dict[str, object] | str
    ) -> None:
        """The caller's own model validates the config field through the config's core schema."""
        outer: type[BaseModel] = create_model("Outer", cfg=(config_cls, ...))
        with pytest.raises(ValidationError) as exc_info:
            if method == "__call__":
                outer(**data)  # type: ignore[arg-type]
            else:
                getattr(outer, method)(data)

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")
        assert all(err["loc"][0] == "cfg" for err in exc_info.value.errors())

    def test_a_model_with_config_fields_keeps_one_schema_definition(self) -> None:
        """The redacting wrapper carries the config's ref, so the config stays one $defs entry."""
        outer: type[BaseModel] = create_model("Outer", a=(RedisBackendConfig, ...), b=(RedisBackendConfig, ...))

        schema = outer.model_json_schema()

        assert list(schema["$defs"]) == ["RedisBackendConfig"]
        assert schema["properties"]["a"] == {"$ref": "#/$defs/RedisBackendConfig"}
        assert str(outer.__pydantic_core_schema__).count("'function-wrap'") == 1

    def test_the_schema_hook_leaves_the_handlers_schema_intact(self) -> None:
        """The ref moves to the wrapper off a copy: the handler's schema may be one pydantic holds elsewhere."""
        handed: dict[str, object] = {"type": "any", "ref": "stub-ref"}

        wrapped = RedisBackendConfig.__get_pydantic_core_schema__(RedisBackendConfig, lambda _: handed)  # type: ignore[arg-type]

        assert handed == {"type": "any", "ref": "stub-ref"}
        assert (wrapped["type"], wrapped.get("ref"), wrapped["schema"].get("ref")) == ("function-wrap", "stub-ref", None)  # type: ignore[typeddict-item]

    def test_a_self_referential_config_builds_its_schema(self) -> None:
        assert list(_SelfReferentialConfig.model_json_schema()["$defs"]) == ["_SelfReferentialConfig"]
        with pytest.raises(ValidationError) as exc_info:
            _SelfReferentialConfig(child={"child": "SECRET_VALUE"})  # type: ignore[arg-type]
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_a_union_member_loc_names_the_config_class(self) -> None:
        """A union member's loc names its validator; a per-process repr would split error grouping."""
        outer: type[BaseModel] = create_model("Outer", cfg=(RedisBackendConfig | CachekitConfig, ...))
        with pytest.raises(ValidationError) as exc_info:
            outer(cfg="SECRET_VALUE")

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")
        assert [err["loc"][:2] for err in exc_info.value.errors()] == [
            ("cfg", "function-wrap[RedisBackendConfig()]"),
            ("cfg", "function-wrap[CachekitConfig()]"),
        ]

    @pytest.mark.parametrize(
        "build",
        [
            lambda data: TypeAdapter(CachekitConfig).validate_strings(data),
            lambda data: create_model("Outer", cfg=(CachekitConfig, ...)).model_validate_strings({"cfg": data}),
        ],
        ids=["type-adapter", "field"],
    )
    def test_strings_mode_repromotion_redacts_the_keys(self, build: Callable[[dict[str, str]], object]) -> None:
        """A model-level error snapshots the whole input dict, and the strings mode never calls __init__."""
        with pytest.raises(ValidationError) as exc_info:
            build({"master_key": _KEY_HEX, "previous_master_keys": _KEY_HEX})

        _assert_no_route_to(exc_info.value, _KEY_HEX)
        assert [err["type"] for err in exc_info.value.errors()] == ["value_error"]

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
    def test_an_error_the_rebuild_cannot_handle_withholds_its_details(
        self, ctx_kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a validator's ctx makes the rebuild raise, that failure's traceback holds the original, raw inputs
        and all. It comes back as one ctx-less error with the details withheld, and one warning names the
        failure's type only, logged with no exception active for a handler's sys.exc_info() to reach."""
        from cachekit.config import validation

        warnings: list[tuple[str, BaseException | None]] = []
        monkeypatch.setattr(validation.logger, "warning", lambda msg, *args: warnings.append((msg % args, sys.exc_info()[1])))
        with pytest.raises(ValidationError) as exc_info:
            _UnrebuildableCtxConfig(ctx_kind=ctx_kind, url="SECRET_VALUE")

        [err] = exc_info.value.errors()
        assert (err["type"], err["loc"], err["msg"]) == ("redaction_failed", (), "Validation failed; details withheld")
        assert "ctx" not in err
        _assert_no_route_to(exc_info.value, "SECRET_VALUE")
        msg = "_UnrebuildableCtxConfig: config validation error details withheld; redacting it raised TypeError"
        assert warnings == [(msg, None)]

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


_CACHEKIT_SRC = pathlib.Path(cachekit.__file__).resolve().parent


def _secret_forms(secret: str | bytes) -> tuple[list[str], list[bytes]]:
    """``secret`` as text and as bytes: a str also as its UTF-8 bytes and, when it is hex, the bytes it
    decodes to; bytes also as their hex."""
    if isinstance(secret, bytes):
        return [secret.hex()], [secret]
    as_bytes = [secret.encode()]
    try:
        as_bytes.append(bytes.fromhex(secret))
    except ValueError:
        pass
    return [secret], as_bytes


def _holds(value: object, texts: list[str], raw: list[bytes], depth: int = 0) -> bool:
    """Whether ``value`` carries the secret the way a frame-locals reporter serialises it: str and bytes by
    content, dict/list/tuple/set by recursing into their items (keys too), anything else by its repr."""
    if isinstance(value, str):
        return any(t in value for t in texts)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return any(r in bytes(value) for r in raw)
    if isinstance(value, dict) and depth < 10:
        return any(_holds(k, texts, raw, depth + 1) or _holds(v, texts, raw, depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)) and depth < 10:
        return any(_holds(item, texts, raw, depth + 1) for item in value)
    return any(t in repr(value) for t in texts)


def _held_exception(value: object) -> BaseException | None:
    """The exception a frame local keeps alive without a tracker serialising it: the local itself, or what a failed
    Future holds. Its own frames are on its traceback, so they still hold whatever they held."""
    if isinstance(value, BaseException):
        return value
    if isinstance(value, (asyncio.Future, concurrent.futures.Future)) and value.done() and not value.cancelled():
        return value.exception()
    return None


def _cachekit_locals_holding(exc: BaseException, secret: str | bytes, *, below_caller: bool = False) -> list[str]:
    """Every ``frame:local`` under src/cachekit/ that holds ``secret`` on the traceback of ``exc`` or of any
    exception reachable from it (``__cause__``/``__context__``, a BackendError's ``original_exception``, and an
    exception or failed Future held in a local of a frame walked), or with ``below_caller`` every frame but this
    test file's, third-party frames included.

    Error trackers capture frame locals by default (Sentry's ``include_local_variables``) and serialise
    containers item by item, and their scrubbers match top-level key names, so a raw key held in any local,
    or inside a dict or list in one, is a key sent off-host. A str secret is matched as text, as UTF-8
    bytes and, when hex, as the bytes it decodes to.
    """
    this_file = pathlib.Path(__file__).resolve()
    texts, raw = _secret_forms(secret)
    found: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending += [current.__cause__, current.__context__, getattr(current, "original_exception", None)]
        tb = current.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            path = pathlib.Path(code.co_filename).resolve()
            if path != this_file if below_caller else path.is_relative_to(_CACHEKIT_SRC):
                for name, value in tb.tb_frame.f_locals.items():
                    if _holds(value, texts, raw):
                        found.append(f"{code.co_name}:{name}")
                    pending.append(_held_exception(value))
            tb = tb.tb_next
    return found


@pytest.mark.unit
class TestRedactingSettingsFrameLocals:
    """No cachekit frame on a raised config error's traceback keeps the raw input (CWE-532)."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: CachekitConfig(master_key=_KEY_HEX, max_value_size=-1),  # type: ignore[arg-type]
            lambda: CachekitConfig(master_key=_KEY_HEX, previous_master_keys=[_KEY_HEX]),  # type: ignore[arg-type,list-item]
            lambda: CachekitConfig.model_validate({"master_key": _KEY_HEX, "max_value_size": -1}),
            lambda: CachekitConfig.model_validate_json(json.dumps({"master_key": _KEY_HEX, "max_value_size": -1})),
            lambda: CachekitConfig.model_validate_strings({"master_key": _KEY_HEX, "max_value_size": "-1"}),
            lambda: CachekitIOBackendConfig(api_key=_KEY_HEX, timeout=-1),  # type: ignore[arg-type]
            lambda: TypeAdapter(CachekitConfig).validate_python(_KEY_HEX),
            lambda: TypeAdapter(CachekitConfig).validate_strings({"master_key": _KEY_HEX, "previous_master_keys": _KEY_HEX}),
            lambda: create_model("Outer", cfg=(CachekitConfig, ...))(cfg=_KEY_HEX),
        ],
        ids=[
            "kwarg",
            "kwarg-repromotion",
            "model_validate",
            "model_validate_json",
            "model_validate_strings",
            "io-api-key",
            "type-adapter",
            "type-adapter-strings",
            "field",
        ],
    )
    def test_programmatic_input_leaves_no_frame_local(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValidationError) as exc_info:
            build()

        assert _cachekit_locals_holding(exc_info.value, _KEY_HEX) == []

    @pytest.mark.parametrize(
        "build",
        [CachekitConfig, CachekitConfig.from_env, singleton.get_settings],
        ids=["constructor", "from_env", "get_settings"],
    )
    def test_env_repromotion_leaves_no_frame_local(self, monkeypatch: pytest.MonkeyPatch, build: Callable[[], object]) -> None:
        """Already clean before the frame-locals fix (the raw err dicts die with _redacted_copy's frame); a guard."""
        monkeypatch.setattr(singleton, "_settings_instance", None)
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _KEY_HEX)
        monkeypatch.setenv("CACHEKIT_PREVIOUS_MASTER_KEYS", _KEY_HEX)
        with pytest.raises(ValidationError) as exc_info:
            build()

        assert _cachekit_locals_holding(exc_info.value, _KEY_HEX) == []

    @pytest.mark.parametrize(
        "build",
        [
            lambda: CachekitConfig.model_validate({"master_key": "ab" * 32, "max_value_size": -1}),
            lambda: CachekitConfig.model_validate_json(json.dumps({"master_key": "ab" * 32, "max_value_size": -1})),
            lambda: CachekitConfig.model_validate_strings({"master_key": "ab" * 32, "max_value_size": "-1"}),
        ],
        ids=["model_validate", "model_validate_json", "model_validate_strings"],
    )
    def test_classmethods_leave_no_frame_local_outside_the_caller(self, build: Callable[[], object]) -> None:
        """The core schema redacts too, but pydantic's own classmethod frame holds the raw input: the
        classmethod overrides keep that frame off the traceback. The input is an inline temporary, so only
        a frame below the caller's could hold it."""
        with pytest.raises(ValidationError) as exc_info:
            build()

        assert _cachekit_locals_holding(exc_info.value, _KEY_HEX, below_caller=True) == []


_API_KEY = "ck_test_frameLocalsApiKey0123456789"  # pragma: allowlist secret
_KEY_BYTES = bytes.fromhex(_KEY_HEX)
_SHORT_KEY_HEX = "cd" * 16
_REDIS_PASSWORD = "frameLocalsRedisPassword"  # pragma: allowlist secret


def _cached() -> int:
    return 1


def _lazy_wrapper_build() -> object:
    """N7: the handler builds its EncryptionWrapper on first use, after the settings it reads went bad."""
    handler = CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_KEY_HEX)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("CACHEKIT_MAX_VALUE_SIZE", "-1")
        singleton.reset_settings()
        return handler.serialize_data({"v": 1})


# Every master_key= but EncryptionWrapper's takes a hex string, and refuses a bytes key with TypeError rather than decode
# it (protocol intent-presets.md, Master Key Input): whatever the encryption setting, and an empty one too, which must not
# fall back to CACHEKIT_MASTER_KEY. Each call passes the key the row name says.
_BYTES_KEY_ROWS: dict[str, Callable[[], object]] = {
    "secure-intent-bytes-key": lambda: cache.secure(master_key=_KEY_BYTES)(_cached),
    "secure-intent-bytearray-key": lambda: cache.secure(master_key=bytearray(_KEY_BYTES))(_cached),
    "secure-intent-empty-bytes-key": lambda: cache.secure(master_key=b"")(_cached),
    "secure-intent-empty-bytearray-key": lambda: cache.secure(master_key=bytearray())(_cached),
    "secure-intent-empty-memoryview-key": lambda: cache.secure(master_key=memoryview(b""))(_cached),
    "bare-encryption-off-bytes-key": lambda: cache(encryption=False, master_key=_KEY_BYTES)(_cached),
    "bare-encryption-bytes-key": lambda: cache(encryption=True, single_tenant_mode=True, master_key=_KEY_BYTES)(_cached),
    "bare-bytes-key": lambda: cache(master_key=_KEY_BYTES)(_cached),
    "secure-config-bytes-key": lambda: DecoratorConfig.secure(master_key=_KEY_BYTES),
    "secure-config-memoryview-key": lambda: DecoratorConfig.secure(master_key=memoryview(_KEY_BYTES)),  # type: ignore[arg-type]
    "encryption-config-bytes-key": lambda: EncryptionConfig(enabled=True, master_key=_KEY_BYTES, single_tenant_mode=True),  # type: ignore[arg-type]
    "encryption-config-positional-bytes-key": lambda: EncryptionConfig(True, _KEY_BYTES),  # type: ignore[arg-type]
    "encryption-config-disabled-bytes-key": lambda: EncryptionConfig(enabled=False, master_key=_KEY_BYTES),  # type: ignore[arg-type]
    "validate-bytes-key": lambda: validate_encryption_config(True, _KEY_BYTES),  # type: ignore[arg-type]
    "validate-bytearray-key": lambda: validate_encryption_config(True, bytearray(_KEY_BYTES)),  # type: ignore[arg-type]
    "validate-encryption-off-bytes-key": lambda: validate_encryption_config(False, _KEY_BYTES),  # type: ignore[arg-type]
    "handler-bytes-key": lambda: CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_KEY_BYTES),  # type: ignore[arg-type]
    "handler-encryption-off-bytes-key": lambda: CacheSerializationHandler(encryption=False, master_key=_KEY_BYTES),  # type: ignore[arg-type]
    # Python 3.14's bytes.fromhex reads ASCII hex bytes, so this one used to work there: hex is a str.
    "handler-ascii-hex-bytes-key": lambda: CacheSerializationHandler(
        encryption=True,
        single_tenant_mode=True,
        master_key=_KEY_HEX.encode(),  # type: ignore[arg-type]
    ),
}


_EntryPointRow = tuple[dict[str, str], Callable[[], object], type[BaseException], str | bytes]

# The lowercase names: urllib.request.getproxies prefers them, and an empty no_proxy drops any NO_PROXY bypass.
_BAD_PROXY_ENV = {"https_proxy": "http://[", "no_proxy": ""}


def _api_key_rows(form: Callable[[str], Any]) -> dict[str, _EntryPointRow]:
    """The entry-point rows that pass an API key, each passing it as ``form`` makes it from a str. A row's secret is the
    str key whatever the form: _cachekit_locals_holding matches a str as text and as its UTF-8 bytes."""
    key = form(_API_KEY)
    return {
        "io-backend-timeout": ({}, lambda: CachekitIOBackend(api_key=key, timeout=-1), ConfigurationError, _API_KEY),
        "io-backend-env-timeout": (
            {"CACHEKIT_TIMEOUT": "-1"},
            lambda: CachekitIOBackend(api_key=key),
            ConfigurationError,
            _API_KEY,
        ),
        "io-backend-url": (
            {},
            lambda: CachekitIOBackend(api_key=key, api_url="https://example.com"),
            ConfigurationError,
            _API_KEY,
        ),
        "io-backend-bad-token": ({}, lambda: CachekitIOBackend(api_key=form(_API_KEY + "\n")), ConfigurationError, _API_KEY),
        "io-backend-with-timeout": ({}, lambda: CachekitIOBackend(api_key=key).with_timeout(-1), ConfigurationError, _API_KEY),
        "io-config-typo": ({}, lambda: DecoratorConfig.io(api_key=key, timeout=-1), ConfigurationError, _API_KEY),
        "io-intent-typo": ({}, lambda: cache.io(api_key=key, timeout=-1)(_cached), ConfigurationError, _API_KEY),
        "io-intent-env-timeout": (
            {"CACHEKIT_TIMEOUT": "-1"},
            lambda: cache.io(api_key=key)(_cached),
            ConfigurationError,
            _API_KEY,
        ),
        # The client is built after the config validates, and a malformed proxy URL fails there.
        "io-backend-bad-proxy": (_BAD_PROXY_ENV, lambda: CachekitIOBackend(api_key=key), LocationParseError, _API_KEY),
        "io-config-bad-proxy": (_BAD_PROXY_ENV, lambda: DecoratorConfig.io(api_key=key), LocationParseError, _API_KEY),
        "io-intent-bad-proxy": (_BAD_PROXY_ENV, lambda: cache.io(api_key=key)(_cached), LocationParseError, _API_KEY),
        # A key passed where no form takes one is still a key.
        "secure-config-misplaced-api-key": (
            {},
            lambda: DecoratorConfig.secure(master_key=_KEY_HEX, api_key=key),
            ConfigurationError,
            _API_KEY,
        ),
        # So is a key under a misspelt name, also when another guard raises before the keyword check.
        "io-intent-misspelt-key": ({}, lambda: cache.io(api_kye=key)(_cached), ConfigurationError, _API_KEY),
        "config-form-misspelt-key-beside-io-backend": (
            {},
            lambda: cache(config=DecoratorConfig.io(api_key=key), backend=None, api_kye=key)(_cached),
            ConfigurationError,
            _API_KEY,
        ),
        "io-intent-misspelt-key-beside-config": (
            {},
            lambda: cache.io(config=DecoratorConfig.minimal(), api_kye=key)(_cached),
            ConfigurationError,
            _API_KEY,
        ),
    }


# Every _api_key_rows row runs with each of these: @cache.io, DecoratorConfig.io and CachekitIOBackend take a bytes key too,
# and hold it only wrapped, as they do a str one.
_API_KEY_FORMS: dict[str, Callable[[str], Any]] = {
    "": str,
    "-bytes-api-key": str.encode,
    "-bytearray-api-key": lambda text: bytearray(text.encode()),
    "-memoryview-api-key": lambda text: memoryview(text.encode()),
}


# (env, call, raised, secret) per public entry point that takes a secret or reads one from the environment.
_ENTRY_POINT_ROWS: dict[str, _EntryPointRow] = {
    **{name + suffix: row for suffix, form in _API_KEY_FORMS.items() for name, row in _api_key_rows(form).items()},
    # A key passed where no form takes one is still a key (LAB-8223).
    "minimal-config-misplaced-key": ({}, lambda: DecoratorConfig.minimal(master_key=_KEY_HEX), ConfigurationError, _KEY_HEX),
    "io-config-misplaced-key": (
        {},
        lambda: DecoratorConfig.io(api_key=_API_KEY, master_key=_KEY_HEX),
        ConfigurationError,
        _KEY_HEX,
    ),
    "config-form-misplaced-key": (
        {},
        lambda: cache(config=DecoratorConfig.minimal(), master_key=_KEY_HEX)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    # So is a key under a misspelt name: every refused value is wrapped, in each dict a frame holds.
    "minimal-config-misspelt-key": ({}, lambda: DecoratorConfig.minimal(master_keey=_KEY_HEX), ConfigurationError, _KEY_HEX),
    "minimal-intent-misspelt-key": ({}, lambda: cache.minimal(master_keey=_KEY_HEX)(_cached), ConfigurationError, _KEY_HEX),
    "secure-intent-misspelt-key": ({}, lambda: cache.secure(master_keey=_KEY_HEX)(_cached), ConfigurationError, _KEY_HEX),
    "bare-misspelt-key": ({}, lambda: cache(master_keey=_KEY_HEX)(_cached), ConfigurationError, _KEY_HEX),
    "bare-call-misspelt-key": ({}, lambda: cache(_cached, master_keey=_KEY_HEX), ConfigurationError, _KEY_HEX),
    "config-form-misspelt-key": (
        {},
        lambda: cache(config=DecoratorConfig.minimal(), master_keey=_KEY_HEX)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    # And when another guard raises before the keyword check: one row per such guard.
    "config-form-misspelt-key-beside-encryption-override": (
        {},
        lambda: cache(config=DecoratorConfig.secure(master_key=_KEY_HEX), encryption=False, master_keey=_KEY_HEX)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "config-form-misspelt-key-beside-integrity-override": (
        {},
        lambda: cache(config=DecoratorConfig.secure(master_key=_KEY_HEX), integrity_checking=False, master_keey=_KEY_HEX)(
            _cached
        ),
        ConfigurationError,
        _KEY_HEX,
    ),
    "minimal-intent-misspelt-key-beside-encrypting-serializer": (
        {},
        lambda: cache.minimal(serializer="encrypted", master_keey=_KEY_HEX)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "bare-misspelt-key-beside-non-config": (
        {},
        lambda: cache(config="minimal", master_keey=_KEY_HEX)(_cached),  # type: ignore[arg-type]
        TypeError,
        _KEY_HEX,
    ),
    "local-intent-misspelt-key": ({}, lambda: cache.local(master_keey=_KEY_HEX)(_cached), TypeError, _KEY_HEX),
    # A refused bytes value is wrapped too: no form takes a bytes key, but a caller may still pass one.
    "minimal-config-misspelt-bytes-key": (
        {},
        lambda: DecoratorConfig.minimal(master_keey=bytes.fromhex(_KEY_HEX)),
        ConfigurationError,
        bytes.fromhex(_KEY_HEX),
    ),
    "minimal-intent-misplaced-bytes-key": (
        {},
        lambda: cache.minimal(master_key=bytes.fromhex(_KEY_HEX))(_cached),
        ConfigurationError,
        bytes.fromhex(_KEY_HEX),
    ),
    "config-form-misspelt-bytes-key-beside-encryption-override": (
        {},
        lambda: cache(config=DecoratorConfig.secure(master_key=_KEY_HEX), encryption=False, master_keey=bytes.fromhex(_KEY_HEX))(
            _cached
        ),
        ConfigurationError,
        bytes.fromhex(_KEY_HEX),
    ),
    # A bytes key is held only wrapped where a form refuses master_key= itself (_BYTES_KEY_ROWS cover the rest).
    "local-intent-bytes-key": ({}, lambda: cache.local(master_key=_KEY_BYTES)(_cached), TypeError, _KEY_HEX),
    "secure-config-bytes-key-beside-misspelt-key": (
        {},
        lambda: DecoratorConfig.secure(master_key=_KEY_BYTES, master_keey=_KEY_HEX),
        ConfigurationError,
        _KEY_HEX,
    ),
    "config-form-encryption-override": (
        {},
        lambda: cache(config=DecoratorConfig.secure(master_key=_KEY_HEX), encryption=False)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "secure-intent-ttl": ({}, lambda: cache.secure(master_key=_KEY_HEX, ttl=-5)(_cached), ValueError, _KEY_HEX),
    "secure-intent-env-size": (
        {"CACHEKIT_MAX_VALUE_SIZE": "-1"},
        lambda: cache.secure(master_key=_KEY_HEX)(_cached),
        ValidationError,
        _KEY_HEX,
    ),
    "secure-intent-env-fail-closed": (
        {"CACHEKIT_ENCRYPTION_FAIL_CLOSED": "notbool"},
        lambda: cache.secure(master_key=_KEY_HEX)(_cached),
        ValidationError,
        _KEY_HEX,
    ),
    "bare-encryption-no-tenant": (
        {},
        lambda: cache(encryption=True, master_key=_KEY_HEX)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "bare-encryption-env-size": (
        {"CACHEKIT_MAX_VALUE_SIZE": "-1"},
        lambda: cache(encryption=True, single_tenant_mode=True, master_key=_KEY_HEX)(_cached),
        ValidationError,
        _KEY_HEX,
    ),
    "handler-env-fail-closed": (
        {"CACHEKIT_ENCRYPTION_FAIL_CLOSED": "notbool"},
        lambda: CacheSerializationHandler(encryption=True, single_tenant_mode=True, master_key=_KEY_HEX),
        ValidationError,
        _KEY_HEX,
    ),
    "wrapper-env-size": (
        {"CACHEKIT_MAX_VALUE_SIZE": "-1"},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX)),
        ValidationError,
        _KEY_HEX,
    ),
    "wrapper-short-previous": (
        {},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX), previous_master_keys=[b"\x01" * 16]),
        KeyringConfigurationError,
        _KEY_HEX,
    ),
    "wrapper-invalid-later-previous": (
        {},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX), previous_master_keys=[b"\x01" * 32, None]),  # type: ignore[list-item]
        TypeError,
        b"\x01" * 32,
    ),
    "wrapper-str-key": ({}, lambda: EncryptionWrapper(master_key=_KEY_HEX), TypeError, _KEY_HEX),  # type: ignore[arg-type]
    "wrapper-long-raw-key": (
        {},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX) + b"\x01"),
        EncryptionError,
        _KEY_HEX,
    ),
    "wrapper-long-raw-previous": (
        {},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX), previous_master_keys=[b"\x01" * 33]),
        KeyringConfigurationError,
        _KEY_HEX,
    ),
    "redis-backend-env-pool": (
        {"CACHEKIT_CONNECTION_POOL_SIZE": "notint"},
        lambda: RedisBackend(redis_url=f"redis://:{_REDIS_PASSWORD}@localhost:6379/0"),
        ValidationError,
        _REDIS_PASSWORD,
    ),
    "redis-backend-bad-port": (
        {},
        lambda: RedisBackend(redis_url=f"redis://:{_REDIS_PASSWORD}@localhost:notaport/0"),
        ValueError,
        _REDIS_PASSWORD,
    ),
    "redis-backend-bad-scheme": (
        {},
        lambda: RedisBackend(redis_url=f"bogus://:{_REDIS_PASSWORD}@localhost:6379/0"),
        ValueError,
        _REDIS_PASSWORD,
    ),
    "redis-backend-bad-ipv6": (
        {},
        lambda: RedisBackend(redis_url=f"redis://:{_REDIS_PASSWORD}@[::1/0"),
        ValueError,
        _REDIS_PASSWORD,
    ),
    "secure-config-ttl": ({}, lambda: DecoratorConfig.secure(master_key=_KEY_HEX, ttl=-5), ValueError, _KEY_HEX),
    "secure-intent-env-key-ttl": (
        {"CACHEKIT_MASTER_KEY": _KEY_HEX},
        lambda: cache.secure(ttl=-5)(_cached),
        ValueError,
        _KEY_HEX,
    ),
    "bare-no-intent": ({}, lambda: cache(master_key=_KEY_HEX)(_cached), ConfigurationError, _KEY_HEX),
    "handler-no-intent": ({}, lambda: CacheSerializationHandler(master_key=_KEY_HEX), ConfigurationError, _KEY_HEX),
    "production-no-intent": (
        {},
        lambda: cache.production(encryption=EncryptionConfig(master_key=_KEY_HEX))(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "wrapper-env-key-short-previous": (
        {"CACHEKIT_MASTER_KEY": _KEY_HEX},
        lambda: EncryptionWrapper(previous_master_keys=[b"\x01" * 16]),
        KeyringConfigurationError,
        _KEY_HEX,
    ),
    "handler-lazy-wrapper": ({}, _lazy_wrapper_build, ValidationError, _KEY_HEX),
    "secure-intent-l1-only": (
        {},
        lambda: cache.secure(master_key=_KEY_HEX, backend=None)(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "secure-intent-encrypting-serializer": (
        {},
        lambda: cache.secure(master_key=_KEY_HEX, serializer=EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX)))(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "bare-encrypting-serializer": (
        {},
        lambda: cache(serializer=EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX)))(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "config-encrypting-serializer": (
        {},
        lambda: cache(config=DecoratorConfig(serializer=EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX))))(_cached),
        ConfigurationError,
        _KEY_HEX,
    ),
    "wrapper-env-previous-is-current": (
        {"CACHEKIT_PREVIOUS_MASTER_KEYS": _KEY_HEX},
        lambda: EncryptionWrapper(master_key=bytes.fromhex(_KEY_HEX)),
        KeyringConfigurationError,
        _KEY_HEX,
    ),
    "secure-intent-short-key": (
        {},
        lambda: cache.secure(master_key=_SHORT_KEY_HEX)(_cached),
        ConfigurationError,
        _SHORT_KEY_HEX,
    ),
    "secure-intent-non-hex-key": ({}, lambda: cache.secure(master_key="zz" * 32)(_cached), ConfigurationError, "zz" * 32),
    "bare-encryption-short-key": (
        {},
        lambda: cache(encryption=True, single_tenant_mode=True, master_key=_SHORT_KEY_HEX)(_cached),
        ConfigurationError,
        _SHORT_KEY_HEX,
    ),
    "validate-env-short-key": (
        {"CACHEKIT_MASTER_KEY": _SHORT_KEY_HEX},
        lambda: validate_encryption_config(True),
        ConfigurationError,
        _SHORT_KEY_HEX,
    ),
}


_CLEARED_ENV = ("CACHEKIT_MASTER_KEY", "CACHEKIT_PREVIOUS_MASTER_KEYS", "CACHEKIT_API_KEY", "CACHEKIT_MAX_VALUE_SIZE")


@pytest.mark.unit
class TestEntryPointFrameLocals:
    """No cachekit frame on an error raised from a public entry point holds a secret it was passed or read
    from the environment (CWE-532): not the raw value, not inside a dict or list, not as bytes."""

    @pytest.mark.parametrize(("env", "call", "raised", "secret"), _ENTRY_POINT_ROWS.values(), ids=_ENTRY_POINT_ROWS.keys())
    def test_raised_error_leaves_no_frame_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        call: Callable[[], object],
        raised: type[BaseException],
        secret: str | bytes,
    ) -> None:
        for name in _CLEARED_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        singleton.reset_settings()
        # A fresh lease cache, so each row builds its own client: a cached one would skip the build that raises.
        monkeypatch.setattr(cachekitio_client, "_leases", cachekitio_client._Leases())

        with pytest.raises(raised) as exc_info:
            call()

        assert _cachekit_locals_holding(exc_info.value, secret) == []

    @pytest.mark.parametrize("env_key", [None, "ef" * 32], ids=["no-env-key", "env-key"])
    @pytest.mark.parametrize("call", _BYTES_KEY_ROWS.values(), ids=_BYTES_KEY_ROWS.keys())
    def test_bytes_key_refusal_quotes_no_key_and_leaves_no_frame_local_below_the_caller(
        self, monkeypatch: pytest.MonkeyPatch, call: Callable[[], object], env_key: str | None
    ) -> None:
        """Every frame below the caller's, not only cachekit's: EncryptionConfig refuses the key before its
        generated __init__, whose frame would hold it raw, runs. A CACHEKIT_MASTER_KEY never stands in for it."""
        for name in _CLEARED_ENV:
            monkeypatch.delenv(name, raising=False)
        if env_key is not None:
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", env_key)
        singleton.reset_settings()

        with pytest.raises(TypeError) as exc_info:
            call()

        assert str(exc_info.value) == _BYTES_KEY_REFUSAL
        assert _cachekit_locals_holding(exc_info.value, _KEY_HEX, below_caller=True) == []
