"""Untrusted-decode bounds: protocol vectors + the SDK-local regression guard.

Why the bound exists and how it works: the ``unpackb_bounded`` docstring in
``cachekit.serializers.base`` (the canonical home). This file pins, so a
msgpack-python bump cannot silently move it:
- every reject vector (reasons depth, overclaim and incomplete) is rejected on every decode path
  by the pre-decode structural guard itself: the error (or one in its cause/context chain) is the guard's own
  ``Unpack failed: MessagePack document ...``, which no decoder produces, so a path that
  skips the guard fails even if msgpack still rejects the bytes later; the peak stays bounded;
- ``validate_data`` (which returns a bool) proves the same through a spy on the guard;
- every accept vector decodes on every path at its declared depth (the bound cannot over-tighten);
- the nesting ceiling is exactly MSGPACK_MAX_NESTING;
- the read path turns a bomb into SerializationError (a controlled miss), not a crash.

Fixture: tests/unit/protocol/fixtures/decode-bounds.json, vendored from
cachekit-io/protocol test-vectors/decode-bounds.json (sha256 pinned below).
Regenerate ONLY by re-copying from the protocol repo — never by hand.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msgpack
import pytest

import cachekit.serializers.base as serializers_base
from cachekit._rust_serializer import ByteStorage, check_msgpack_structure
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.interop import decode_interop_value
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import MSGPACK_MAX_NESTING, SerializationError, unpackb_bounded
from cachekit.serializers.interop_serializer import InteropSerializer
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper
from tests.utils.tracemalloc_isolation import measure_in_subprocess

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "decode-bounds.json"
FIXTURE_SHA256 = "c52c27f724fe138e63440dc0306936b48fe389e2bd823c058b86b30d854e2e2e"  # pragma: allowlist secret
VECTORS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
EXPECTED_COUNTS = {"reject_vectors": 19, "accept_vectors": 3}

# Only check_msgpack_structure raises this. msgpack's own rejections read "Unpack failed: incomplete
# input" and the like, and the interop wrapper's "not a single well-formed MessagePack document" wraps
# whatever it caught — so a substring match on "MessagePack document" would pass with the guard gone.
GUARD_ERROR_PREFIX = "Unpack failed: MessagePack document "

# Peak transient heap a rejected decode may cost: a small constant (tracemalloc + unpackb
# overhead) plus a few multiples of the input. Unguarded, the nested_array32_input_len vector
# peaks at ~8000x its input, so this discriminates by three orders of magnitude.
PEAK_BUDGET = 2 * 1024 * 1024
PEAK_PER_INPUT_BYTE = 4


def _envelope(payload: bytes, format_id: str = "msgpack") -> bytes:
    return bytes(ByteStorage("msgpack").store(payload, format_id))


CACHE_KEY = "ns:decode:bounds"


@functools.lru_cache(maxsize=2)
def _frame_template(serializer: str = "default") -> tuple[dict[str, Any], str]:
    _, metadata, serializer_name = SerializationWrapper.unwrap(
        CacheSerializationHandler(serializer).serialize_data({"t": 1}, cache_key=CACHE_KEY)
    )
    return metadata, serializer_name


def _forged_entry(payload: bytes, serializer: str = "default") -> bytes:
    """A genuine CK v3 frame with its payload swapped — the backend-write attacker's move."""
    metadata, serializer_name = _frame_template(serializer)
    return SerializationWrapper.wrap(_envelope(payload), metadata, serializer_name)


# Every path that decodes backend-supplied MessagePack. Each must reach unpackb_bounded.
DECODE_PATHS: dict[str, Callable[[bytes], Any]] = {
    "unpackb_bounded": lambda b: unpackb_bounded(b, raw=False),
    "interop": decode_interop_value,
    "interop_serializer": InteropSerializer().deserialize,
    "standard/plain": StandardSerializer(enable_integrity_checking=False).deserialize,
    "standard/envelope": lambda b: StandardSerializer().deserialize(_envelope(b)),
    "auto/plain": AutoSerializer(enable_integrity_checking=False).deserialize,
    "auto/envelope": lambda b: AutoSerializer().deserialize(_envelope(b)),
    "handler/default": lambda b: CacheSerializationHandler().deserialize_data(_forged_entry(b), cache_key=CACHE_KEY),
    "handler/auto": lambda b: CacheSerializationHandler("auto").deserialize_data(_forged_entry(b, "auto"), cache_key=CACHE_KEY),
    # An unencrypted interop entry is the bare document — no frame to forge (pinned in TestOwnedBounds).
    "handler/interop": lambda b: CacheSerializationHandler(interop_mode=True, encryption=False).deserialize_data(
        b, cache_key=CACHE_KEY
    ),
}


def _assert_guard_rejected(exc: BaseException) -> None:
    """Fail unless the structural guard's own error is exc or in its cause/context chain."""
    link: BaseException | None = exc
    while link is not None:
        if str(link).startswith(GUARD_ERROR_PREFIX):
            return
        link = link.__cause__ or link.__context__
    pytest.fail(f"rejected, but not by the structural guard: {exc!r}")


def _peak_of(fn: Callable[..., Any], *args: Any) -> int:
    tracemalloc.start()
    try:
        with contextlib.suppress(ValueError, SerializationError):  # the tests assert the rejection in-process
            fn(*args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _measure_peaks() -> dict[str, int]:
    """Every heap peak this file asserts on, keyed by case. Runs only via measure_in_subprocess."""
    cases = {
        f"{path}:{v['name']}": (fn, bytes.fromhex(v["input_hex"]))
        for path, fn in DECODE_PATHS.items()
        for v in VECTORS["reject_vectors"]
    }
    validate_data = AutoSerializer(enable_integrity_checking=False).validate_data
    cases |= {f"validate_data:{v['name']}": (validate_data, bytes.fromhex(v["input_hex"])) for v in VECTORS["reject_vectors"]}
    return {case: _peak_of(fn, data) for case, (fn, data) in cases.items()}


@pytest.fixture(scope="module")
def peaks() -> dict[str, int]:
    return measure_in_subprocess(_measure_peaks)


def _vector_ids(group: str) -> list[str]:
    return [v["name"] for v in VECTORS[group]]


def _reject_vector(name: str) -> bytes:
    return bytes.fromhex(next(v["input_hex"] for v in VECTORS["reject_vectors"] if v["name"] == name))


class TestFixtureIsTheVendoredProtocolFile:
    def test_sha256_and_counts(self) -> None:
        assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256
        assert {g: len(VECTORS[g]) for g in EXPECTED_COUNTS} == EXPECTED_COUNTS
        assert VECTORS["spec"] == "spec/interop-mode.md#decode-bounds"

    @pytest.mark.parametrize("vector", VECTORS["reject_vectors"], ids=_vector_ids("reject_vectors"))
    def test_reject_reasons_are_known_and_hold(self, vector: dict[str, Any]) -> None:
        # The reasons are maintainer notes, but a new one is a new rule the guard may not enforce yet:
        # an unknown reason fails here, so adopting it is a conscious change. Each known one is checked
        # against the vector's own numbers (decode-bounds.json "rules").
        checks = {
            "depth": vector["nesting_depth"] > 1024,
            "overclaim": vector["declared_slots"] > vector["input_len"] - 1,
            "incomplete": vector["declared_slots"] <= vector["input_len"] - 1,
        }
        assert vector["reject_reasons"], vector["name"]
        for reason in vector["reject_reasons"]:
            assert reason in checks, f"{vector['name']}: unknown reject reason {reason!r}"
            assert checks[reason], f"{vector['name']}: does not satisfy its {reason!r} reason"
        assert len(bytes.fromhex(vector["input_hex"])) == vector["input_len"]


@pytest.mark.parametrize("path", DECODE_PATHS)
class TestProtocolVectors:
    @pytest.mark.parametrize("vector", VECTORS["reject_vectors"], ids=_vector_ids("reject_vectors"))
    def test_reject_vector_is_rejected_with_bounded_peak(self, path: str, vector: dict[str, Any], peaks: dict[str, int]) -> None:
        data = bytes.fromhex(vector["input_hex"])
        # The only rejections the read path maps to a controlled miss; any other type propagates.
        with pytest.raises((ValueError, SerializationError)) as excinfo:
            DECODE_PATHS[path](data)
        _assert_guard_rejected(excinfo.value)
        peak = peaks[f"{path}:{vector['name']}"]
        assert peak < PEAK_BUDGET + PEAK_PER_INPUT_BYTE * len(data), f"{vector['name']}: {path} peaked at {peak} bytes"

    @pytest.mark.parametrize("vector", VECTORS["accept_vectors"], ids=_vector_ids("accept_vectors"))
    def test_accept_vector_decodes(self, path: str, vector: dict[str, Any]) -> None:
        data = bytes.fromhex(vector["input_hex"])
        value = DECODE_PATHS[path](data)
        # Every accept vector nests through the first element of a list, or the only value of a map.
        depth = 0
        while isinstance(value, (list, dict)):
            depth, value = depth + 1, next(iter(value.values() if isinstance(value, dict) else value), None)
        assert depth == vector["nesting_depth"]


class TestOwnedBounds:
    """SDK-local guards that go beyond the shared vectors."""

    def test_nesting_ceiling_is_exactly_the_pinned_constant(self) -> None:
        # The walk rejects one level past MSGPACK_MAX_NESTING; a document AT the ceiling
        # must still decode, so the constant may not exceed msgpack-python's C stack.
        at_bound = b"\x91" * MSGPACK_MAX_NESTING + b"\xc0"
        # Walked iteratively, not compared with `==`: nested-list equality recurses in CPython
        # too, and would blow the same recursion limit this test is bounding.
        value: object = unpackb_bounded(at_bound)
        depth = 0
        while isinstance(value, list):
            assert len(value) == 1
            depth, value = depth + 1, value[0]
        assert depth == MSGPACK_MAX_NESTING
        assert value is None
        with pytest.raises(ValueError, match=f"nests deeper than {MSGPACK_MAX_NESTING} levels"):
            unpackb_bounded(b"\x91" * (MSGPACK_MAX_NESTING + 1) + b"\xc0")

    def test_trailing_bytes_still_rejected(self) -> None:
        with pytest.raises(msgpack.exceptions.ExtraData):
            unpackb_bounded(b"\xc0\xc0")

    def test_mutable_exporters_are_accepted(self) -> None:
        # A bytearray (or a memoryview over one) is snapshotted so the walk and the decode see one
        # immutable document; a memoryview of bytes stays zero-copy. All three must decode.
        doc = msgpack.packb({"t": 1})
        assert unpackb_bounded(bytearray(doc), raw=False) == {"t": 1}
        assert unpackb_bounded(memoryview(bytearray(doc)), raw=False) == {"t": 1}
        assert unpackb_bounded(memoryview(doc)[0:], raw=False) == {"t": 1}

    def test_memoryview_shapes_and_formats_decode_like_bytes(self) -> None:
        # The Rust walk takes PyBuffer<u8>; msgpack takes any itemsize-1 buffer. Views are normalised
        # to a flat "B" view first so the two agree, len(data) is the byte count the caps need, and a
        # legitimate document is never rejected for the shape or format of the view it arrived in.
        doc = next(
            d
            for d in (msgpack.packb({"k": b"x" * m, "l": [1, 2, 3]}, use_bin_type=True) for m in range(1, 9))
            if len(d) % 8 == 0
        )
        expected = msgpack.unpackb(doc, raw=False)
        views = {
            "signed char": memoryview(doc).cast("b"),
            "char": memoryview(doc).cast("c"),
            "2-D bytes": memoryview(doc).cast("B", shape=[len(doc) // 8, 8]),
            "uint16": memoryview(doc).cast("H"),
        }
        for name, view in views.items():
            assert unpackb_bounded(view, raw=False) == expected, name
        # A non-contiguous view has no flat form: it is copied, then decoded like the bytes it selects.
        interleaved = bytes(b for pair in zip(doc, doc, strict=True) for b in pair)
        assert unpackb_bounded(memoryview(interleaved)[::2], raw=False) == expected

    # One exact-width document per fixed-width marker family: float32/64, uint8..64, int8..64,
    # fixext 1/2/4/8/16, ext8/16/32 (2-byte payload), str8/16/32 + fixstr, bin8/16/32.
    FIXED_WIDTH_DOCS = [
        b"\xca" + b"\x00" * 4,
        b"\xcb" + b"\x00" * 8,
        b"\xcc\x00",
        b"\xcd\x00\x00",
        b"\xce" + b"\x00" * 4,
        b"\xcf" + b"\x00" * 8,
        b"\xd0\x00",
        b"\xd1\x00\x00",
        b"\xd2" + b"\x00" * 4,
        b"\xd3" + b"\x00" * 8,
        b"\xd4\x01\x00",
        b"\xd5\x01\x00\x00",
        b"\xd6\x01" + b"\x00" * 4,
        b"\xd7\x01" + b"\x00" * 8,
        b"\xd8\x01" + b"\x00" * 16,
        b"\xc7\x02\x01\x00\x00",
        b"\xc8\x00\x02\x01\x00\x00",
        b"\xc9\x00\x00\x00\x02\x01\x00\x00",
        b"\xa1x",
        b"\xd9\x01x",
        b"\xda\x00\x01x",
        b"\xdb\x00\x00\x00\x01x",
        b"\xc4\x01x",
        b"\xc5\x00\x01x",
        b"\xc6\x00\x00\x00\x01x",
    ]

    @pytest.mark.parametrize("doc", FIXED_WIDTH_DOCS, ids=lambda d: f"0x{d[0]:02x}")
    def test_every_marker_is_walked_to_its_exact_width(self, doc: bytes) -> None:
        # Exact length passes the walk; one byte short is a truncation; a trailing byte reaches the
        # decoder as ExtraData — together they pin that the walk consumed exactly the marker's width.
        check_msgpack_structure(doc, MSGPACK_MAX_NESTING)
        with pytest.raises(ValueError, match="Unpack failed"):
            check_msgpack_structure(doc[:-1], MSGPACK_MAX_NESTING)
        with pytest.raises(msgpack.exceptions.ExtraData):
            unpackb_bounded(doc + b"\xc0")

    def test_reserved_marker_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved marker 0xc1"):
            check_msgpack_structure(b"\xc1", MSGPACK_MAX_NESTING)

    def test_plain_path_miss_does_not_depend_on_numpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Without the [data] extra (the free-threaded CI lane) a forged plain entry must still be a
        # SerializationError — not the RuntimeError a NumPy fallback raises for a missing numpy.
        monkeypatch.setattr("cachekit.serializers.auto_serializer.HAS_NUMPY", False)
        with pytest.raises(SerializationError, match="not a decodable MessagePack payload"):
            AutoSerializer(enable_integrity_checking=False).deserialize(_reject_vector("bin32_overclaim"))

    @pytest.mark.parametrize("vector", VECTORS["reject_vectors"], ids=_vector_ids("reject_vectors"))
    def test_validate_data_reports_a_bomb_as_invalid_via_the_guard(
        self, vector: dict[str, Any], peaks: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Python-only validate_data is a decode path too: a bomb must read as invalid (not raise),
        # and the walk must have stopped it before the decoder pre-allocated ~8000x the input. The
        # bool hides which check rejected, so a spy on the guard (looked up as a module global of
        # serializers.base by unpackb_bounded at call time) records its error.
        guard, guard_errors = serializers_base.check_msgpack_structure, []

        def spy(data: Any, max_depth: int) -> None:
            try:
                guard(data, max_depth)
            except ValueError as e:
                guard_errors.append(e)
                raise

        monkeypatch.setattr(serializers_base, "check_msgpack_structure", spy)
        serializer = AutoSerializer(enable_integrity_checking=False)
        assert serializer.validate_data(msgpack.packb({"t": 1})) is True
        bomb = bytes.fromhex(vector["input_hex"])
        assert serializer.validate_data(bomb) is False
        assert guard_errors, "validate_data rejected the bomb without the structural guard raising"
        peak = peaks[f"validate_data:{vector['name']}"]
        assert peak < PEAK_BUDGET + PEAK_PER_INPUT_BYTE * len(bomb), f"validate_data peaked at {peak} bytes"

    def test_unencrypted_interop_entry_is_the_bare_document(self) -> None:
        # Why "handler/interop" feeds the vector bytes straight to deserialize_data: there is no frame.
        handler = CacheSerializationHandler(interop_mode=True, encryption=False)
        assert handler.serialize_data({"t": 1}, cache_key=CACHE_KEY) == msgpack.packb({"t": 1})

    @pytest.mark.parametrize("vector", VECTORS["reject_vectors"], ids=_vector_ids("reject_vectors"))
    @pytest.mark.parametrize("original_type", ["dataframe", "series"])
    def test_bomb_behind_a_dataframe_or_series_frame_is_a_controlled_miss(
        self, original_type: str, vector: dict[str, Any]
    ) -> None:
        # AutoSerializer's metadata routes decode outside the verified-envelope normaliser, through its own
        # unpackb_bounded call; the guard must still be what rejects, and the rejection must reach the
        # handler as SerializationError (evict + tamper hook), never a bare ValueError. The message match
        # keeps a "Serializer mismatch" error from faking a pass.
        metadata, serializer_name = _frame_template("auto")
        bomb = bytes.fromhex(vector["input_hex"])
        # The envelope carries the SAME format as the header: this test is about the decode bound,
        # not about format disagreement (which fails closed earlier — see the test below). Tagging
        # the envelope "msgpack" under a columnar header made this an accidental disagreement case.
        frame = SerializationWrapper.wrap(
            _envelope(bomb, original_type), {**metadata, "original_type": original_type}, serializer_name
        )
        with pytest.raises(SerializationError, match=f"failed to decode as {original_type}") as excinfo:
            CacheSerializationHandler("auto").deserialize_data(frame, cache_key=CACHE_KEY)
        _assert_guard_rejected(excinfo.value)

    def test_envelope_format_disagreeing_with_the_header_is_a_controlled_miss(self) -> None:
        # A format disagreement must reach the handler as SerializationError (evict + tamper hook),
        # never as a decoded value. One columnar case: the check is a string inequality, not a branch
        # on which type — tests/unit/test_auto_serializer_mutation_and_corruption.py covers the shapes.
        metadata, serializer_name = _frame_template("auto")
        frame = SerializationWrapper.wrap(
            _envelope(msgpack.packb({"t": 1})), {**metadata, "original_type": "dataframe"}, serializer_name
        )
        with pytest.raises(SerializationError, match="disagrees with header format"):
            CacheSerializationHandler("auto").deserialize_data(frame, cache_key=CACHE_KEY)
