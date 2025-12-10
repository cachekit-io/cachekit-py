"""Encryption Wrapper for Zero-Knowledge Encryption

Provides client-side encryption on top of any SerializerProtocol implementation.
Uses AES-256-GCM for authenticated encryption with per-tenant key isolation.

Architectural Note:
    EncryptionWrapper is a Decorator pattern implementation, not a serialization format.
    It wraps any SerializerProtocol (AutoSerializer, OrjsonSerializer, ArrowSerializer)
    and adds an encryption layer. This enables zero-knowledge caching where the backend
    never sees plaintext, regardless of data type (JSON, DataFrames, MessagePack, etc.).
"""

import logging
from typing import Any, Optional

# Import zero-knowledge encryption from Rust
from cachekit._rust_serializer import ZeroKnowledgeEncryptor, derive_tenant_keys
from cachekit.config import get_settings

from .auto_serializer import AutoSerializer
from .base import SerializationError, SerializationMetadata, SerializerProtocol

logger = logging.getLogger(__name__)


class EncryptionError(SerializationError):
    """Exception raised when encryption operations fail."""

    pass


class EncryptionWrapper:
    """Encryption wrapper that composes any SerializerProtocol with AES-256-GCM encryption layer.

    Architectural Note:
        This is a wrapper using the Decorator pattern, NOT a serialization format.
        It delegates serialization to ANY SerializerProtocol implementation and adds
        an encryption layer on top. This design allows clean separation of concerns
        (serialization vs encryption) and enables zero-knowledge caching for any data type.

    Features:
    - Client-side AES-256-GCM encryption (zero-knowledge)
    - Hardware-accelerated via ring library
    - Per-tenant cryptographic isolation
    - Domain separation for security
    - Works with ANY serializer (AutoSerializer, OrjsonSerializer, ArrowSerializer)

    Security Model:
    - Storage backend never sees plaintext
    - Each tenant gets different derived keys
    - Domain separation prevents key confusion attacks
    - Authentication tags prevent tampering

    Design Pattern:
        Uses Decorator pattern to add encryption behavior without modifying the base serializer.
        Can wrap any SerializerProtocol (MessagePack, JSON, Arrow) for zero-knowledge caching.

    Examples:
        Basic encryption/decryption roundtrip with cache_key binding (AAD v0x03):

        >>> wrapper = EncryptionWrapper(master_key=b"a" * 32, tenant_id="test-tenant")
        >>> data = {"user_id": 123, "secret": "password"}
        >>> cache_key = "users:123:profile"
        >>> encrypted, metadata = wrapper.serialize(data, cache_key=cache_key)
        >>> isinstance(encrypted, bytes)
        True
        >>> metadata.encrypted
        True
        >>> metadata.encryption_algorithm
        'AES-256-GCM'

        Decryption with same cache_key succeeds:

        >>> decrypted = wrapper.deserialize(encrypted, metadata, cache_key=cache_key)
        >>> decrypted == data
        True

        Decryption with WRONG cache_key fails (ciphertext substitution attack detected):

        >>> wrong_key = "users:456:profile"
        >>> wrapper.deserialize(encrypted, metadata, cache_key=wrong_key)  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        EncryptionError: Decryption failed: ...

        Encryption disabled mode (for testing):

        >>> unencrypted_wrapper = EncryptionWrapper(enable_encryption=False)
        >>> raw_data, raw_meta = unencrypted_wrapper.serialize({"test": 1}, cache_key="")
        >>> raw_meta.encrypted
        False
    """

    def __init__(
        self,
        serializer: Optional[SerializerProtocol] = None,
        master_key: Optional[bytes] = None,
        tenant_id: str = "default",
        enable_encryption: bool = True,
    ):
        """Initialize encryption wrapper.

        Args:
            serializer: Any SerializerProtocol implementation to wrap with encryption.
                           Defaults to AutoSerializer (MessagePack + compression).
            master_key: 256-bit master key for encryption. If None, reads from environment.
            tenant_id: Tenant identifier for key isolation
            enable_encryption: Whether to enable encryption (can disable for testing)
        """
        self.tenant_id = tenant_id
        self.enable_encryption = enable_encryption

        # Initialize base serializer (defaults to AutoSerializer for backward compatibility)
        self.serializer = serializer if serializer is not None else AutoSerializer()

        # Initialize encryption components (Optional - only set if encryption enabled)
        self.encryptor: Optional[ZeroKnowledgeEncryptor] = None
        self.tenant_keys: Optional[Any] = None  # TenantKeys from Rust
        self.encryption_key_fingerprint: Optional[str] = None

        # Setup encryption if enabled
        if self.enable_encryption:
            self._setup_encryption(master_key)
        else:
            logger.warning("Encryption disabled - using AutoSerializer only. Data will NOT be encrypted!")

    def _setup_encryption(self, master_key: Optional[bytes]) -> None:
        """Setup encryption components with key derivation."""
        # Get master key from settings if not provided
        if master_key is None:
            settings = get_settings()
            if not settings.master_key:
                raise EncryptionError(
                    "Master key required. Set REDIS_CACHE_MASTER_KEY environment variable or pass master_key parameter."
                )
            try:
                master_key = bytes.fromhex(settings.master_key.get_secret_value())
            except ValueError as e:
                raise EncryptionError(f"Invalid master key format in configuration: {e}") from e

        if len(master_key) < 32:
            raise EncryptionError("Master key must be at least 32 bytes (256 bits)")

        # Initialize encryptor
        self.encryptor = ZeroKnowledgeEncryptor()

        # Derive tenant-specific keys with domain separation
        try:
            self.tenant_keys = derive_tenant_keys(master_key, self.tenant_id)
            if self.tenant_keys is None:
                raise RuntimeError("Key derivation failed")

            # Get key fingerprints for metadata (fingerprints are safe to expose)
            self.encryption_key_fingerprint = self.tenant_keys.encryption_fingerprint().hex()
            if self.encryption_key_fingerprint is None:
                raise RuntimeError("Fingerprint generation failed")
            if self.encryptor is None:
                raise RuntimeError("Encryptor initialization failed")

            logger.info(
                f"Encryption initialized for tenant '{self.tenant_id}' "
                f"(key fingerprint: {self.encryption_key_fingerprint[:12]}..., "
                f"hardware acceleration: {self.encryptor.hardware_acceleration_enabled()})"
            )
        except Exception as e:
            raise EncryptionError(f"Failed to derive tenant keys: {e}") from e

    def serialize(self, obj: Any, cache_key: str = "") -> tuple[bytes, SerializationMetadata]:
        """Serialize and encrypt an object with cache_key binding.

        Args:
            obj: Object to serialize and encrypt
            cache_key: Cache key for AAD binding (SECURITY CRITICAL for encryption).
                      Prevents ciphertext substitution attacks (Protocol v1.0.1, Section 5.6).
                      Empty string allowed only when encryption is disabled.

        Returns:
            Tuple of (encrypted_data, metadata_with_encryption_info)

        Raises:
            ValueError: If cache_key is empty when encryption is enabled
            TypeError: If cache_key is not a string
            EncryptionError: If encryption fails

        Examples:
            Serialize with encryption enabled:

            >>> wrapper = EncryptionWrapper(master_key=b"a" * 32, tenant_id="acme-corp")
            >>> encrypted, meta = wrapper.serialize({"api_key": "secret"}, cache_key="config:api")
            >>> meta.encrypted
            True
            >>> meta.tenant_id
            'acme-corp'

            Empty cache_key raises ValueError (SECURITY: prevents AAD bypass):

            >>> wrapper.serialize({"data": 1}, cache_key="")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            ValueError: cache_key is required when encryption is enabled...

            Non-string cache_key raises TypeError (SECURITY: type safety):

            >>> wrapper.serialize({"data": 1}, cache_key=123)  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            TypeError: cache_key must be a string...
        """
        # First serialize with base serializer
        try:
            raw_data, raw_metadata = self.serializer.serialize(obj)
        except Exception as e:
            raise SerializationError(f"Serialization failed: {e}") from e

        # If encryption is disabled, return raw data with modified metadata
        if not self.enable_encryption:
            return raw_data, raw_metadata

        # SECURITY: Validate cache_key type and value when encryption is enabled
        if not isinstance(cache_key, str):
            raise TypeError(
                f"cache_key must be a string, got {type(cache_key).__name__}. "
                "AAD v0x03 requires a string cache_key for ciphertext binding."
            )
        if not cache_key:
            raise ValueError(
                "cache_key is required when encryption is enabled. "
                "AAD v0x03 requires cache_key binding to prevent ciphertext substitution attacks."
            )

        # Encryption is enabled - encryptor and tenant_keys must be initialized
        if self.encryptor is None:
            raise RuntimeError("Encryptor must be initialized when encryption is enabled")
        if self.tenant_keys is None:
            raise RuntimeError("Tenant keys must be initialized when encryption is enabled")
        if self.encryption_key_fingerprint is None:
            raise RuntimeError("Key fingerprint must be set when encryption is enabled")

        # Encrypt the serialized data
        try:
            # Create Additional Authenticated Data (AAD) v0x03 with cache_key binding
            aad = self._create_aad(raw_metadata, cache_key)

            # Encrypt using tenant keys (keys remain in Rust memory, never copied to Python)
            encrypted_data = self.encryptor.encrypt_with_keys(raw_data, aad, self.tenant_keys)

            # Create enhanced metadata with encryption information
            # Note: format preserved from base serializer (could be MSGPACK, ORJSON, ARROW, etc.)
            encrypted_metadata = SerializationMetadata(
                serialization_format=raw_metadata.format,  # Preserve underlying wire format
                encoding=raw_metadata.encoding,
                compressed=raw_metadata.compressed,
                original_type=raw_metadata.original_type,
                encrypted=True,  # Security layer indicator (orthogonal to format)
                tenant_id=self.tenant_id,
                encryption_algorithm="AES-256-GCM",
                key_fingerprint=self.encryption_key_fingerprint,
            )

            return encrypted_data, encrypted_metadata

        except Exception as e:
            raise EncryptionError(f"Encryption failed: {e}") from e

    def deserialize(self, data: bytes, metadata: SerializationMetadata, cache_key: str = "") -> Any:
        """Decrypt and deserialize data with cache_key verification.

        Args:
            data: Encrypted data to deserialize
            metadata: Serialization metadata with encryption info
            cache_key: Cache key for AAD verification (SECURITY CRITICAL for encryption).
                      Must match the cache_key used during encryption.
                      Empty string allowed only when data is not encrypted.

        Returns:
            Deserialized object

        Raises:
            ValueError: If cache_key is empty when data is encrypted
            TypeError: If cache_key is not a string
            EncryptionError: If decryption fails (including cache_key mismatch)

        Examples:
            Successful roundtrip with matching cache_key:

            >>> wrapper = EncryptionWrapper(master_key=b"b" * 32, tenant_id="tenant-1")
            >>> original = {"items": [1, 2, 3], "total": 6}
            >>> enc_data, enc_meta = wrapper.serialize(original, cache_key="cart:user:42")
            >>> wrapper.deserialize(enc_data, enc_meta, cache_key="cart:user:42")
            {'items': [1, 2, 3], 'total': 6}

            Wrong cache_key causes AES-GCM authentication failure (ciphertext substitution detected):

            >>> wrapper.deserialize(enc_data, enc_meta, cache_key="cart:user:99")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            EncryptionError: Decryption failed: ...

            Tenant mismatch raises EncryptionError:

            >>> other_wrapper = EncryptionWrapper(master_key=b"b" * 32, tenant_id="tenant-2")
            >>> other_wrapper.deserialize(enc_data, enc_meta, cache_key="cart:user:42")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            EncryptionError: Tenant mismatch: data encrypted for 'tenant-1', but current tenant is 'tenant-2'
        """
        # Handle unencrypted data (fallback case)
        # Check encrypted flag (orthogonal to format - encryption is a wrapper, not a format)
        if not metadata.encrypted:
            return self.serializer.deserialize(data, metadata)

        # SECURITY: Validate cache_key type and value for encrypted data
        if not isinstance(cache_key, str):
            raise TypeError(
                f"cache_key must be a string, got {type(cache_key).__name__}. "
                "AAD v0x03 verification requires a string cache_key."
            )
        if not cache_key:
            raise ValueError(
                "cache_key is required to decrypt data. "
                "AAD v0x03 verification requires cache_key to prevent ciphertext substitution attacks."
            )

        # Verify encryption is available
        if not self.enable_encryption:
            raise EncryptionError("Received encrypted data but encryption is disabled")

        # Encryption is enabled - encryptor and tenant_keys must be initialized
        if self.encryptor is None:
            raise RuntimeError("Encryptor must be initialized when encryption is enabled")
        if self.tenant_keys is None:
            raise RuntimeError("Tenant keys must be initialized when encryption is enabled")

        # Verify tenant match for security
        if metadata.tenant_id != self.tenant_id:
            raise EncryptionError(
                f"Tenant mismatch: data encrypted for '{metadata.tenant_id}', but current tenant is '{self.tenant_id}'"
            )

        # Verify key fingerprint for rotation detection
        if metadata.key_fingerprint != self.encryption_key_fingerprint:
            metadata_fp = metadata.key_fingerprint[:12] if metadata.key_fingerprint else "unknown"
            current_fp = self.encryption_key_fingerprint[:12] if self.encryption_key_fingerprint else "unknown"
            logger.warning(
                f"Key fingerprint mismatch: data encrypted with key "
                f"{metadata_fp}..., current key is {current_fp}... (key rotation?)"
            )
            # Continue anyway - might be old data with rotated key

        try:
            # Create the same AAD used during encryption (with cache_key binding)
            raw_metadata = SerializationMetadata(
                serialization_format=metadata.format,  # Preserve original wire format from base serializer
                encoding=metadata.encoding,
                compressed=metadata.compressed,
                original_type=metadata.original_type,
            )
            aad = self._create_aad(raw_metadata, cache_key)

            # Decrypt using tenant keys (keys remain in Rust memory, never copied to Python)
            # NOTE: If cache_key doesn't match the one used during encryption,
            # the AAD will be different and AES-GCM authentication will fail.
            # This is the SECURITY mechanism that detects ciphertext substitution.
            decrypted_data = self.encryptor.decrypt_with_keys(data, aad, self.tenant_keys)

            # Deserialize the decrypted data using base serializer
            return self.serializer.deserialize(decrypted_data, raw_metadata)

        except Exception as e:
            raise EncryptionError(f"Decryption failed: {e}") from e

    def _create_aad(self, metadata: SerializationMetadata, cache_key: str) -> bytes:
        """Create length-prefixed AAD v0x03 with cache_key binding.

        Format: [version_byte(0x03)][len1(4)][tenant_id][len2(4)][cache_key][len3(4)][format][len4(4)][compressed]
        - Version byte: 0x03 (includes cache_key binding)
        - Each length is 4-byte big-endian integer
        - Mathematically impossible to collide (unambiguous parsing)

        SECURITY CRITICAL (Protocol v1.0.1, Section 5.6):
            cache_key binding prevents ciphertext substitution attacks (CVSS 8.5).
            Without this, an attacker within the same tenant can swap ciphertext
            between cache keys - the ciphertext would decrypt successfully because
            same tenant = same derived key.

        Args:
            metadata: Serialization metadata (format, compressed, etc.)
            cache_key: Full cache key - binds ciphertext to this specific key

        Returns:
            AAD bytes for AES-256-GCM authentication

        Examples:
            AAD starts with version byte 0x03:

            >>> from cachekit.serializers.base import SerializationFormat, SerializationMetadata
            >>> wrapper = EncryptionWrapper(master_key=b"c" * 32, tenant_id="demo")
            >>> meta = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, compressed=True)
            >>> aad = wrapper._create_aad(meta, cache_key="session:abc123")
            >>> aad[0] == 0x03  # Version byte
            True

            AAD can be parsed back to original components:

            >>> parsed = wrapper._parse_aad(aad)
            >>> parsed["tenant_id"]
            'demo'
            >>> parsed["cache_key"]
            'session:abc123'
            >>> parsed["format"]
            'msgpack'
            >>> parsed["compressed"]
            'True'

            Different cache_keys produce different AAD (prevents ciphertext swapping):

            >>> aad1 = wrapper._create_aad(meta, "key:user:1")
            >>> aad2 = wrapper._create_aad(meta, "key:user:2")
            >>> aad1 != aad2
            True
        """
        # Encode components as bytes - cache_key is SECURITY CRITICAL
        components = [
            self.tenant_id.encode("utf-8"),
            cache_key.encode("utf-8"),  # SECURITY: prevents ciphertext substitution attacks
            metadata.format.value.encode("utf-8"),
            str(metadata.compressed).encode("utf-8"),
        ]

        if metadata.original_type:
            components.append(metadata.original_type.encode("utf-8"))

        # Version byte 0x03 + length-prefixed encoding
        aad = bytes([0x03])  # Version 0x03: includes cache_key binding
        for component in components:
            aad += len(component).to_bytes(4, "big") + component

        return aad

    def _parse_aad(self, aad: bytes) -> dict[str, Optional[str]]:
        """Parse length-prefixed AAD for validation and debugging.

        Args:
            aad: Length-prefixed AAD bytes (with version byte)

        Returns:
            Dictionary with parsed components. For v0x03:
            tenant_id, cache_key, format, compressed, [original_type]

        Raises:
            ValueError: If AAD format is invalid

        Examples:
            Parse valid AAD v0x03:

            >>> from cachekit.serializers.base import SerializationFormat, SerializationMetadata
            >>> wrapper = EncryptionWrapper(master_key=b"d" * 32, tenant_id="acme")
            >>> meta = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, compressed=False)
            >>> aad = wrapper._create_aad(meta, "orders:12345")
            >>> result = wrapper._parse_aad(aad)
            >>> result["tenant_id"]
            'acme'
            >>> result["cache_key"]
            'orders:12345'
            >>> result["compressed"]
            'False'

            Reject old AAD versions (no backward compatibility - greenfield):

            >>> old_aad = bytes([0x02]) + b"\\x00\\x00\\x00\\x04test"  # Version 0x02
            >>> wrapper._parse_aad(old_aad)  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            ValueError: Unsupported AAD version: 0x2 (expected 0x03)

            Reject empty AAD:

            >>> wrapper._parse_aad(b"")  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            ValueError: Empty AAD
        """
        if len(aad) == 0:
            raise ValueError("Empty AAD")

        # Check version byte
        version = aad[0]
        if version != 0x03:
            raise ValueError(f"Unsupported AAD version: {version:#x} (expected 0x03)")

        # Parse length-prefixed components
        components = []
        offset = 1  # Skip version byte

        while offset < len(aad):
            # Read 4-byte length
            if offset + 4 > len(aad):
                raise ValueError("Invalid AAD: truncated length field")

            length = int.from_bytes(aad[offset : offset + 4], "big")
            offset += 4

            # Read component
            if offset + length > len(aad):
                raise ValueError("Invalid AAD: truncated component")

            component = aad[offset : offset + length].decode("utf-8")
            components.append(component)
            offset += length

        # Return parsed components for v0x03:
        # [tenant_id, cache_key, format, compressed, [original_type]]
        return {
            "tenant_id": components[0] if len(components) > 0 else "",
            "cache_key": components[1] if len(components) > 1 else "",
            "format": components[2] if len(components) > 2 else "",
            "compressed": components[3] if len(components) > 3 else "",
            "original_type": components[4] if len(components) > 4 else None,
        }

    @property
    def is_encryption_enabled(self) -> bool:
        """Check if encryption is currently enabled.

        Examples:
            >>> wrapper = EncryptionWrapper(master_key=b"e" * 32)
            >>> wrapper.is_encryption_enabled
            True
            >>> disabled = EncryptionWrapper(enable_encryption=False)
            >>> disabled.is_encryption_enabled
            False
        """
        return self.enable_encryption

    @property
    def hardware_acceleration_enabled(self) -> bool:
        """Check if hardware acceleration is enabled.

        Returns True if AES-NI or equivalent CPU instructions are available.

        Examples:
            >>> wrapper = EncryptionWrapper(master_key=b"f" * 32)
            >>> isinstance(wrapper.hardware_acceleration_enabled, bool)
            True
        """
        if not self.enable_encryption:
            return False
        if self.encryptor is None:
            raise RuntimeError("Encryptor must be initialized when encryption is enabled")
        return self.encryptor.hardware_acceleration_enabled()

    def get_encryption_info(self) -> dict[str, Any]:
        """Get information about encryption status and configuration.

        Returns a dictionary with encryption details for debugging and logging.

        Examples:
            Encryption enabled - full info:

            >>> wrapper = EncryptionWrapper(master_key=b"g" * 32, tenant_id="prod-tenant")
            >>> info = wrapper.get_encryption_info()
            >>> info["enabled"]
            True
            >>> info["algorithm"]
            'AES-256-GCM'
            >>> info["tenant_id"]
            'prod-tenant'
            >>> info["library"]
            'ring (Rust)'
            >>> "key_fingerprint" in info  # Fingerprint included (safe to log)
            True

            Encryption disabled - minimal info:

            >>> disabled = EncryptionWrapper(enable_encryption=False)
            >>> disabled.get_encryption_info()
            {'enabled': False, 'reason': 'Encryption disabled or not available'}
        """
        if not self.enable_encryption:
            return {"enabled": False, "reason": "Encryption disabled or not available"}

        if self.encryption_key_fingerprint is None:
            raise RuntimeError("Key fingerprint must be set when encryption is enabled")

        return {
            "enabled": True,
            "tenant_id": self.tenant_id,
            "algorithm": "AES-256-GCM",
            "key_fingerprint": self.encryption_key_fingerprint,
            "hardware_acceleration": self.hardware_acceleration_enabled,
            "library": "ring (Rust)",
        }
