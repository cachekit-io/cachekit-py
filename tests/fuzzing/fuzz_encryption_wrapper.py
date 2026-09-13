#!/usr/bin/env python3
"""Atheris fuzz target for EncryptionWrapper (Python → Encryption boundary)."""

from __future__ import annotations

import contextlib
import copy
import importlib
import sys
import uuid

import atheris

# Pre-import third-party deps so instrument_imports() below skips them
# (already in sys.modules = not instrumented) — we fuzz cachekit's code;
# third-party coverage is not the goal, and atheris-instrumented third-party
# bytecode is a proven startup-crash class: instrumented pydantic segfaults
# CPython 3.11 in _decorators.merge_seqs during pydantic_settings'
# CLI-provider model construction (pulled in transitively via
# cachekit.hiredis_compat). That SIGSEGV killed every nightly target during
# startup, before a single fuzz iteration (LAB-1140/LAB-2528). Optional deps
# use suppress: absent is fine, instrumented is the trap.
for _mod in (
    "pydantic",
    "pydantic_settings",
    "numpy",
    "pandas",
    "pyarrow",
    "redis",
    "msgpack",
    "xxhash",
    "prometheus_client",
):
    with contextlib.suppress(ImportError):
        importlib.import_module(_mod)

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
    # Explicit raise, not assert: -O / PYTHONOPTIMIZE strips assert, which would
    # leave this target reporting no crashes while verifying nothing. Matches
    # the AAD-binding and tenant-isolation oracles below, which already raise.
    if decrypted != payload:
        raise AssertionError("Encryption roundtrip failed: data mismatch")

    # AAD binding: a different cache_key must fail authentication.
    try:
        wrapper_a.deserialize(encrypted, metadata, cache_key=cache_key + "x")
        raise RuntimeError("AAD binding failed: decrypt succeeded with wrong cache_key")
    except DecryptionAuthenticationError:
        pass

    # Tenant isolation. Metadata is cleartext an attacker controls, so the
    # honest cross-tenant check FORGES it (tenant_id + key_fingerprint claim
    # tenant B) — that gets past the unauthenticated metadata comparisons and
    # must still die at the AES-GCM layer, where tenant separation is real
    # (HKDF tenant-derived key + tenant-bound AAD).
    if tenant_a != tenant_b:
        wrapper_b = EncryptionWrapper(master_key=_MASTER_KEY, tenant_id=tenant_b)
        encrypted_b, metadata_b = wrapper_b.serialize(payload, cache_key=cache_key)
        if encrypted == encrypted_b:
            raise AssertionError("Tenant isolation failed: ciphertexts match")
        forged = copy.copy(metadata)
        forged.tenant_id = metadata_b.tenant_id
        forged.key_fingerprint = metadata_b.key_fingerprint
        try:
            wrapper_b.deserialize(encrypted, forged, cache_key=cache_key)
            raise RuntimeError("Tenant isolation failed: cross-tenant decrypt succeeded")
        except DecryptionAuthenticationError:
            pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
