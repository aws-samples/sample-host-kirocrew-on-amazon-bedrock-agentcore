from __future__ import annotations

import hashlib

import pytest
from cryptography.exceptions import InvalidTag
from kirocrew_agentcore_persistence.crypto import EncryptedBlob, IntegrityError, SandboxCipher


def test_cipher_requires_key_and_sandbox_and_is_deterministic_per_sandbox() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        SandboxCipher(b"short", "sandbox")
    with pytest.raises(ValueError, match="sandbox ID"):
        SandboxCipher(b"k" * 32, "")
    cipher = SandboxCipher(b"k" * 32, "sandbox-a")
    first = cipher.encrypt(b"durable content")
    second = cipher.encrypt(b"durable content")
    assert first == second
    assert first.digest == hashlib.sha256(b"durable content").hexdigest()
    assert cipher.decrypt(first) == b"durable content"
    assert SandboxCipher(b"k" * 32, "sandbox-b").encrypt(b"durable content") != first


def test_cipher_rejects_truncation_and_authentication_failure() -> None:
    cipher = SandboxCipher(b"k" * 32, "sandbox")
    with pytest.raises(IntegrityError, match="truncated"):
        cipher.decrypt(EncryptedBlob("0" * 64, b"short"))
    encrypted = cipher.encrypt(b"content")
    tampered = EncryptedBlob(encrypted.digest, encrypted.ciphertext[:-1] + b"\x00")
    with pytest.raises(IntegrityError, match="authentication"):
        cipher.decrypt(tampered)


def test_cipher_checks_plaintext_digest_after_authenticated_decryption() -> None:
    cipher = SandboxCipher(b"k" * 32, "sandbox")

    class DigestMismatchAead:
        def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
            assert nonce and ciphertext and aad
            return b"different"

    cipher._aead = DigestMismatchAead()  # type: ignore[assignment]
    with pytest.raises(IntegrityError, match="digest"):
        cipher.decrypt(EncryptedBlob("0" * 64, b"n" * 12 + b"ciphertext"))


def test_cipher_maps_invalid_tag_from_aead() -> None:
    cipher = SandboxCipher(b"k" * 32, "sandbox")

    class InvalidAead:
        def decrypt(self, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
            raise InvalidTag

    cipher._aead = InvalidAead()  # type: ignore[assignment]
    with pytest.raises(IntegrityError, match="authentication"):
        cipher.decrypt(EncryptedBlob("0" * 64, b"n" * 12 + b"ciphertext"))
