"""
PII encryption helper (AES-256-GCM).
All encryption/decryption of sensitive fields (Aadhaar, PAN, bank details)
goes through this module — nothing else touches cryptography directly.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

settings = get_settings()

_NONCE_SIZE = 12  # 96-bit nonce recommended for AES-GCM


def _get_key() -> bytes:
    """Derive a 32-byte key from the configured ENCRYPTION_KEY."""
    raw = settings.ENCRYPTION_KEY.encode()
    if len(raw) < 32:
        raw = raw.ljust(32, b"\x00")
    return raw[:32]


def encrypt(plaintext: str) -> str:
    """Encrypt plaintext → base64-encoded (nonce + ciphertext)."""
    key = _get_key()
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(ciphertext_b64: str) -> str:
    """Decrypt base64-encoded (nonce + ciphertext) → plaintext."""
    key = _get_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce, ct = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()
