"""
CRITICAL PATH TEST: the AES-GCM decrypt bindings read buffer-protocol ciphertext without a copy.

``decrypt_with_keys``, ``TenantKeyring.decrypt_at`` and ``TenantKeyring.decrypt`` take the
ciphertext through the same ``bytes_view`` helper as ``ByteStorage.retrieve``: a read-only
``memoryview`` of ``bytes`` (what ``SerializationWrapper.unwrap_metadata`` returns) is borrowed,
and any other buffer (a ``bytearray``, a view of one, a spoofed exporter) is copied, because the
module runs without a GIL on free-threaded builds and only immutable ``bytes`` is safe to borrow.
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
