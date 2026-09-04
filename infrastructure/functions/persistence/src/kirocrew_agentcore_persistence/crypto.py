from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class IntegrityError(ValueError):
    """Encrypted content failed authentication or digest verification."""


@dataclass(frozen=True, slots=True)
class EncryptedBlob:
    digest: str
    ciphertext: bytes


class SandboxCipher:
    def __init__(self, key: bytes, sandbox_id: str) -> None:
        if len(key) != 32 or not sandbox_id:
            raise ValueError("A 32-byte key and sandbox ID are required.")
        self._key = key
        self._sandbox_id = sandbox_id
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> EncryptedBlob:
        digest = hashlib.sha256(plaintext).hexdigest()
        nonce = hmac.new(self._key, digest.encode(), hashlib.sha256).digest()[:12]
        aad = self._associated_data(digest)
        return EncryptedBlob(digest, nonce + self._aead.encrypt(nonce, plaintext, aad))

    def decrypt(self, blob: EncryptedBlob) -> bytes:
        if len(blob.ciphertext) < 13:
            raise IntegrityError("Encrypted blob is truncated.")
        nonce, ciphertext = blob.ciphertext[:12], blob.ciphertext[12:]
        try:
            plaintext = self._aead.decrypt(
                nonce,
                ciphertext,
                self._associated_data(blob.digest),
            )
        except InvalidTag as error:
            raise IntegrityError("Encrypted blob authentication failed.") from error
        if not hmac.compare_digest(hashlib.sha256(plaintext).hexdigest(), blob.digest):
            raise IntegrityError("Encrypted blob digest does not match plaintext.")
        return plaintext

    def _associated_data(self, digest: str) -> bytes:
        return f"kirocrew-checkpoint-v1\x00{self._sandbox_id}\x00{digest}".encode()
