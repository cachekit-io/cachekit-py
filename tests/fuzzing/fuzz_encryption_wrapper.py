#!/usr/bin/env python3
"""Atheris fuzz target for EncryptionWrapper (Python → Encryption boundary)."""

from __future__ import annotations

import sys
import uuid

import atheris

# Pre-import third-party deps so instrument_imports() below skips them
# (already in sys.modules = not instrumented). Atheris-instrumented pydantic
# bytecode segfaults CPython 3.11 in _decorators.merge_seqs during
# pydantic_settings' CLI-provider model construction (pulled in transitively
# via cachekit.hiredis_compat) — the SIGSEGV killed every nightly target
# during startup, before a single fuzz iteration (LAB-1140/LAB-2528). We fuzz
# cachekit's code; third-party coverage is not the goal.
import pydantic  # noqa: F401
import pydantic_settings  # noqa: F401

with atheris.instrument_imports():
    from cachekit.serializers.encryption_wrapper import (
        DecryptionAuthenticationError,
        EncryptionWrapper,
    )

# Fixed test key for reproducibility (mirrors the doctest fixtures).
_MASTER_KEY = b"0" * 32


def TestOneInput(data: bytes) -> None:
    """Fuzz encrypt/decrypt roundtrip, AAD cache-key binding, and tenant isolation."""
    fdp = atheris.FuzzedDataProvider(data)

    payload = bytes(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 4096)))
    tenant_a = str(uuid.UUID(bytes=bytes(fdp.ConsumeBytes(16)).ljust(16, b"\0")))
    tenant_b = str(uuid.UUID(bytes=bytes(fdp.ConsumeBytes(16)).ljust(16, b"\0")))
    # Prefix guarantees the non-empty cache_key serialize() requires.
    cache_key = "ns:fuzz:" + fdp.ConsumeUnicodeNoSurrogates(64)

    wrapper_a = EncryptionWrapper(master_key=_MASTER_KEY, tenant_id=tenant_a)

    # Roundtrip under the same tenant + cache_key must be lossless.
    encrypted, metadata = wrapper_a.serialize(payload, cache_key=cache_key)
    decrypted = wrapper_a.deserialize(encrypted, metadata, cache_key=cache_key)
    assert decrypted == payload, "Encryption roundtrip failed: data mismatch"

    # AAD binding: a different cache_key must fail authentication.
    try:
        wrapper_a.deserialize(encrypted, metadata, cache_key=cache_key + "x")
        raise RuntimeError("AAD binding failed: decrypt succeeded with wrong cache_key")
    except DecryptionAuthenticationError:
        pass

    # Tenant isolation: different tenant → different ciphertext, and
    # cross-tenant decryption must fail authentication.
    if tenant_a != tenant_b:
        wrapper_b = EncryptionWrapper(master_key=_MASTER_KEY, tenant_id=tenant_b)
        encrypted_b, _ = wrapper_b.serialize(payload, cache_key=cache_key)
        assert encrypted != encrypted_b, "Tenant isolation failed: ciphertexts match"
        try:
            wrapper_b.deserialize(encrypted, metadata, cache_key=cache_key)
            raise RuntimeError("Tenant isolation failed: cross-tenant decrypt succeeded")
        except DecryptionAuthenticationError:
            pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
