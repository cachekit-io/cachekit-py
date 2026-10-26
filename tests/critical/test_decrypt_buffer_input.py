"""
CRITICAL PATH TEST: the AES-GCM decrypt bindings read buffer-protocol ciphertext without a copy.

``decrypt_with_keys``, ``TenantKeyring.decrypt_at`` and ``TenantKeyring.decrypt`` read their
ciphertext through ``ciphertext_view``. Exact ``bytes`` and exact ``memoryview``, the shapes a
read hands the wrapper, go through the ``bytes_view`` helper ``ByteStorage.retrieve`` uses: a
read-only ``B``/``c`` ``memoryview`` of ``bytes`` (what ``SerializationWrapper.unwrap_metadata``
returns for a ``bytes`` frame) is borrowed, and a view of a ``bytearray``, a strided view or a
spoofed exporter is copied, because the module runs without a GIL on free-threaded builds and only
immutable ``bytes`` is safe to borrow. A ``memoryview`` of another format, and every other input
(a ``bytes`` subclass, a ``bytearray``, a custom exporter), is read by ``bytes(obj)`` itself, so
it decrypts exactly as it did under the ``bytes(data)`` coercion the bindings replaced.
``retrieve`` and ``check_msgpack_structure`` stay strict on purpose.

``_bytes_view_borrows`` pins what ``bytes_view`` decides for an input; the ``bytes(obj)`` route is
outside it and is pinned by the round-trip matrix and the parity and wrapper tests below. The
probe does not pin how a binding uses the view: that the bindings pass it to the cipher without
copying it is held by review of ``rust/src/python_bindings.rs``. The tracemalloc test pins only
the Python side.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from cachekit._rust_serializer import Keyring, ZeroKnowledgeEncryptor, _bytes_view_borrows, derive_tenant_keys
from cachekit.serializers.encryption_wrapper import EncryptionWrapper
from cachekit.serializers.wrapper import SerializationWrapper
from tests.utils.tracemalloc_isolation import measure_in_subprocess

pytestmark = pytest.mark.critical

CURRENT, RETIRED = b"c" * 32, b"r" * 32
TENANT = "tenant-buffer-input"
AAD = b"aad"
PLAINTEXT = b"buffer-protocol-ciphertext" * 40
KEY = "ns:buf:func:m.f:args:" + "0" * 64 + ":0"


def _frame_view(payload: bytes) -> memoryview:
    """The read-only memoryview slice of ``bytes`` that ``unwrap_metadata`` hands the decrypt path."""
    frame = SerializationWrapper.wrap(payload, {"format": "msgpack"}, "default")
    view, _, _ = SerializationWrapper.unwrap_metadata(frame)
    assert isinstance(view, memoryview) and view.readonly and isinstance(view.obj, bytes)
    return view


INPUTS = {
    "bytes": bytes,
    "bytearray": bytearray,
    "memoryview_of_bytes": lambda ct: memoryview(ct),
    "unwrap_memoryview_slice": _frame_view,
    "memoryview_of_bytearray": lambda ct: memoryview(bytearray(ct)),
    "readonly_view_of_bytearray": lambda ct: memoryview(bytearray(ct)).toreadonly(),
    # Not a B/c buffer, so it is read as raw bytes, as the replaced bytes(data) coercion read it.
    "signed_byte_view": lambda ct: memoryview(ct).cast("b"),
    "signed_byte_view_of_bytearray": lambda ct: memoryview(bytearray(ct)).cast("b"),
}


def _decrypt_with_keys(ct: Any) -> bytes:
    encryptor = ZeroKnowledgeEncryptor()
    return encryptor.decrypt_with_keys(ct, AAD, derive_tenant_keys(CURRENT, TENANT))


def _decrypt_at(ct: Any) -> bytes:
    return Keyring(CURRENT, [RETIRED]).for_tenant(TENANT).decrypt_at(1, ZeroKnowledgeEncryptor(), ct, AAD)


def _keyring_decrypt(ct: Any) -> bytes:
    return Keyring(CURRENT, [RETIRED]).for_tenant(TENANT).decrypt(ZeroKnowledgeEncryptor(), ct, AAD)


# Each method with the key its ciphertext must be sealed under: decrypt_at(1) reads the retired
# entry, and the sequential decrypt falls through the current key to it.
METHODS = {
    "decrypt_with_keys": (_decrypt_with_keys, CURRENT),
    "decrypt_at": (_decrypt_at, RETIRED),
    "keyring_decrypt": (_keyring_decrypt, RETIRED),
}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("make_input", INPUTS.values(), ids=INPUTS.keys())
def test_decrypt_round_trips_every_buffer_input(method: str, make_input: Any) -> None:
    decrypt, sealing_key = METHODS[method]
    ct = ZeroKnowledgeEncryptor().encrypt_with_keys(PLAINTEXT, AAD, derive_tenant_keys(sealing_key, TENANT))

    assert decrypt(make_input(ct)) == PLAINTEXT


@pytest.mark.parametrize("method", METHODS)
def test_tampered_buffer_input_still_fails_authentication(method: str) -> None:
    """Borrowing changes no verification: a flipped tag byte in a borrowed view still fails."""
    decrypt, sealing_key = METHODS[method]
    ct = bytearray(ZeroKnowledgeEncryptor().encrypt_with_keys(PLAINTEXT, AAD, derive_tenant_keys(sealing_key, TENANT)))
    ct[-1] ^= 0x01

    with pytest.raises(ValueError):
        decrypt(_frame_view(bytes(ct)))


@pytest.mark.parametrize("method", METHODS)
def test_non_buffer_input_is_refused(method: str) -> None:
    decrypt, _ = METHODS[method]
    with pytest.raises(TypeError):
        decrypt("not a buffer")


class _DiscardOnRelease:
    """Signed-byte exporter that wipes its storage whenever an export is released."""

    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)

    def __buffer__(self, flags: int) -> memoryview:
        return memoryview(self.data).cast("b")

    def __release_buffer__(self, view: memoryview) -> None:
        view.release()
        self.data = bytearray(len(self.data))


class _SingleExport:
    """Signed-byte exporter that refuses a second export while one is active."""

    def __init__(self, data: bytes) -> None:
        self.data, self.active = bytearray(data), False

    def __buffer__(self, flags: int) -> memoryview:
        if self.active:
            raise BufferError("one export at a time")
        self.active = True
        return memoryview(self.data).cast("b")

    def __release_buffer__(self, view: memoryview) -> None:
        view.release()
        self.active = False


class _RefusesExport:
    """Refuses its buffer, but ``bytes()`` reads it through ``__bytes__``."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __buffer__(self, flags: int) -> memoryview:
        raise BufferError("no export")

    def __bytes__(self) -> bytes:
        return self.data


class _BytesWithDunder(bytes):
    """A ``bytes`` subclass whose ``__bytes__`` holds the ciphertext; ``bytes()`` prefers it."""

    def __new__(cls, data: bytes) -> _BytesWithDunder:
        obj = super().__new__(cls, b"\x00" * len(data))
        obj.ciphertext = data  # type: ignore[attr-defined]
        return obj

    def __bytes__(self) -> bytes:
        return self.ciphertext  # type: ignore[attr-defined]


EXPORTERS = {
    "discard_on_release": _DiscardOnRelease,
    "single_export": _SingleExport,
    "refuses_export": _RefusesExport,
    "bytes_subclass_with_dunder_bytes": _BytesWithDunder,
}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("make_exporter", EXPORTERS.values(), ids=EXPORTERS.keys())
def test_other_inputs_decrypt_exactly_as_bytes_reads_them(method: str, make_exporter: Any) -> None:
    """Anything but exact ``bytes`` or ``memoryview`` is read by ``bytes(obj)`` itself, once: no
    format probe first, so an exporter that wipes on release or allows one export at a time, or
    one ``bytes()`` reads through ``__bytes__``, decrypts as it did under ``bytes(data)``."""
    if sys.version_info < (3, 12) and make_exporter is not _BytesWithDunder:
        pytest.skip("__buffer__ protocol requires Python 3.12+")
    decrypt, sealing_key = METHODS[method]
    ct = ZeroKnowledgeEncryptor().encrypt_with_keys(PLAINTEXT, AAD, derive_tenant_keys(sealing_key, TENANT))

    assert decrypt(make_exporter(ct)) == PLAINTEXT


def test_wrapper_read_paths_decrypt_a_signed_byte_frame_view() -> None:
    """A signed-byte view of an authentic frame keeps its ``b`` format through ``unwrap_metadata``;
    every wrapper read path still decrypts it, as it did when the wrapper coerced with bytes()."""
    writer = EncryptionWrapper(master_key=RETIRED, tenant_id=TENANT, previous_master_keys=[])
    ciphertext, meta = writer.serialize({"v": 7}, cache_key=KEY)
    frame = SerializationWrapper.wrap(ciphertext, meta.to_dict(), "encrypted")
    current = EncryptionWrapper(master_key=RETIRED, tenant_id=TENANT, previous_master_keys=[])
    rotated = EncryptionWrapper(master_key=CURRENT, tenant_id=TENANT, previous_master_keys=[RETIRED])

    reads = {
        "decrypt_with_keys": lambda view, m: current.deserialize(view, m, cache_key=KEY),
        "decrypt_at": lambda view, m: rotated.deserialize(view, m, cache_key=KEY),
        "decrypt": lambda view, m: rotated.deserialize_without_key_identity(view, m, cache_key=KEY),
    }
    for name, read in reads.items():
        view, metadata, _ = SerializationWrapper.unwrap_metadata(memoryview(frame).cast("b"))
        assert isinstance(view, memoryview) and view.format == "b"
        assert read(view, metadata) == {"v": 7}, name


class TestBorrowVersusCopy:
    """White-box pin of the branch ``bytes_view`` takes: tracemalloc cannot see a Rust-side copy."""

    def test_bytes_is_borrowed(self) -> None:
        assert _bytes_view_borrows(b"ciphertext")

    def test_unwrap_memoryview_slice_is_borrowed(self) -> None:
        assert _bytes_view_borrows(_frame_view(b"ciphertext" * 10))

    def test_memoryview_of_bytearray_is_copied(self) -> None:
        assert not _bytes_view_borrows(memoryview(bytearray(b"ciphertext")))

    def test_readonly_view_of_bytearray_is_copied(self) -> None:
        """``readonly`` describes the view, not the storage: another thread can still write the bytearray."""
        assert not _bytes_view_borrows(memoryview(bytearray(b"ciphertext")).toreadonly())

    def test_bytearray_is_copied(self) -> None:
        assert not _bytes_view_borrows(bytearray(b"ciphertext"))

    def test_strided_view_is_copied(self) -> None:
        assert not _bytes_view_borrows(memoryview(b"c-i-p-h-e-r")[::2])

    def test_bytes_subclass_with_lying_len_reads_real_length(self) -> None:
        """The borrowed length comes from the object's real size, never from ``__len__``: a
        subclass that over-reports would panic the slice (an abort), and one that under-reports
        would truncate the ciphertext into an authentication failure."""
        ct = ZeroKnowledgeEncryptor().encrypt_with_keys(PLAINTEXT, AAD, derive_tenant_keys(CURRENT, TENANT))

        class Long(bytes):
            def __len__(self) -> int:
                return 1_000_000

        class Short(bytes):
            def __len__(self) -> int:
                return 1

        for liar in (Long(ct), Short(ct)):
            assert _bytes_view_borrows(liar)
            assert _decrypt_with_keys(liar) == PLAINTEXT

    def test_spoofed_obj_is_copied(self) -> None:
        """An exporter whose ``.obj`` names a decoy ``bytes`` fails the containment proof."""
        if sys.version_info < (3, 12):
            pytest.skip("__buffer__ protocol requires Python 3.12+")

        class Spoof:
            obj = b"decoy-bytes-not-the-buffer"

            def __buffer__(self, flags: int) -> memoryview:
                return memoryview(bytearray(b"ciphertext")).toreadonly()

        assert not _bytes_view_borrows(Spoof())


class _HeapAtCall:
    """Delegates to a real encryptor or keyring, recording the traced Python heap when a decrypt
    binding is entered: a ``bytes(data)`` coercion in the wrapper is live at that moment."""

    def __init__(self, real: Any, log: dict[str, int]) -> None:
        self._real, self._log = real, log

    def __getattr__(self, name: str) -> Any:
        import tracemalloc

        method = getattr(self._real, name)

        def call(*args: Any) -> Any:
            self._log[name] = tracemalloc.get_traced_memory()[0]
            return method(*args)

        return call


def _measure_wrapper_ciphertext_copies() -> dict:
    """Python heap held at each decrypt binding's entry on the wrapper's three read paths, as a
    multiple of the payload, each fed the memoryview ``unwrap_metadata`` returns."""
    import gc
    import os
    import tracemalloc

    payload = os.urandom(8 * 1024 * 1024)
    writer = EncryptionWrapper(master_key=RETIRED, tenant_id=TENANT, previous_master_keys=[])
    ciphertext, meta = writer.serialize(payload, cache_key=KEY)
    frame = SerializationWrapper.wrap(ciphertext, meta.to_dict(), "encrypted")
    del ciphertext

    log: dict[str, int] = {}
    current = EncryptionWrapper(master_key=RETIRED, tenant_id=TENANT, previous_master_keys=[])
    current.encryptor = _HeapAtCall(current.encryptor, log)  # type: ignore[assignment]
    rotated = EncryptionWrapper(master_key=CURRENT, tenant_id=TENANT, previous_master_keys=[RETIRED])
    rotated._keyring = _HeapAtCall(rotated._keyring, log)  # type: ignore[assignment]

    reads = {
        "decrypt_with_keys": lambda view, m: current.deserialize(view, m, cache_key=KEY),
        "decrypt_at": lambda view, m: rotated.deserialize(view, m, cache_key=KEY),
        "decrypt": lambda view, m: rotated.deserialize_without_key_identity(view, m, cache_key=KEY),
    }
    roundtrip = {}
    for name, read in reads.items():
        view, metadata, _ = SerializationWrapper.unwrap_metadata(frame)
        gc.collect()
        tracemalloc.start()
        roundtrip[name] = read(view, metadata) == payload
        tracemalloc.stop()
    return {"roundtrip": roundtrip, "ratio": {name: log[name] / len(payload) for name in reads}}


def test_wrapper_read_paths_add_no_python_ciphertext_copy() -> None:
    """The wrapper passes ``unwrap``'s memoryview straight to the binding on all three read paths
    (current key, rotation-window entry, interop): no ``bytes(data)`` copy is live when the
    binding is called. A coercion creeping back adds ~1x the payload here."""
    measured = measure_in_subprocess(_measure_wrapper_ciphertext_copies)

    assert measured["roundtrip"] == {"decrypt_with_keys": True, "decrypt_at": True, "decrypt": True}
    for name, ratio in measured["ratio"].items():
        assert ratio < 0.5, f"{name}: {ratio:.2f}x payload on the Python heap at the binding call — a ciphertext copy"
