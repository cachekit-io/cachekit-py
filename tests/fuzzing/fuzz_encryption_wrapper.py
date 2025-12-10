#!/usr/bin/env python3
"""Atheris fuzz target for EncryptionWrapper (Python → Encryption boundary)."""

from __future__ import annotations

import os
import sys
import uuid

import atheris

with atheris.instrument_imports():
    from cachekit.serializers.encryption_wrapper import EncryptionWrapper


def TestOneInput(data: bytes) -> None:
    """Fuzz EncryptionWrapper encrypt/decrypt with tenant isolation."""
    fdp = atheris.FuzzedDataProvider(data)

    # Get master key from environment or use a test key
    master_key_hex = os.environ.get("CACHEKIT_MASTER_KEY")
    if master_key_hex:
        master_key = bytes.fromhex(master_key_hex)
    else:
        # Use a fixed test key for reproducibility
        master_key = b"0" * 32

    try:
        serializer = EncryptionWrapper(master_key=master_key)

        # Fuzz payload and tenant ID
        payload = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 4096))
        tenant_id_bytes = fdp.ConsumeBytes(16)
        tenant_id = str(uuid.UUID(bytes=tenant_id_bytes))

        # Test encryption roundtrip
        encrypted = serializer.serialize(payload, tenant_id=tenant_id)
        decrypted = serializer.deserialize(encrypted, tenant_id=tenant_id)

        # Verify roundtrip
        assert decrypted == payload, "Roundtrip failed: data mismatch"

        # Test tenant isolation: same data with different tenant_id should produce different ciphertext
        other_tenant_bytes = fdp.ConsumeBytes(16)
        other_tenant_id = str(uuid.UUID(bytes=other_tenant_bytes))
        if tenant_id != other_tenant_id:
            encrypted_other = serializer.serialize(payload, tenant_id=other_tenant_id)
            assert encrypted != encrypted_other, "Tenant isolation failed: ciphertexts match"
    except (ValueError, OverflowError, RuntimeError, AttributeError):
        # Expected exceptions for malformed input
        pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
