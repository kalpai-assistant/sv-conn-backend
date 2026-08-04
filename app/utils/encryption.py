"""
PII encryption helper (AES-256-GCM).
All encryption/decryption of sensitive fields (Aadhaar, PAN, bank details)
goes through this module — nothing else touches cryptography directly.
"""

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)

_NONCE_SIZE = 12  # 96-bit nonce recommended for AES-GCM

# Dev-only fallback used ONLY when ENVIRONMENT == "dev" AND the configured
# ENCRYPTION_KEY isn't a valid 32 bytes. Lets contributors boot the app
# locally without provisioning a real key. In staging/prod this branch is
# unreachable — _resolve_key() raises before hitting it.
# Exactly 32 bytes. Rotating it changes nothing in prod; dev DBs are throwaway.
_DEV_FALLBACK_KEY = b"eazyserv-dev-only-key-32-bytes!!"
assert len(_DEV_FALLBACK_KEY) == 32

_PROD_ENVS = frozenset({"staging", "production"})


def _resolve_key() -> bytes:
    """Return the 32-byte AES key, or raise if the config is unsafe.

    Accepted ENCRYPTION_KEY formats:
      - raw 32-byte UTF-8 string (len == 32)
      - base64-encoded 32 bytes (len == 44, decodes cleanly to 32 bytes)

    Anything else is a config bug. In staging/production this raises so the
    process fails at import time — silently padding with null bytes (the
    previous behaviour) downgraded AES-256 to something with much less
    entropy and masked the misconfiguration. In dev an unset / wrong key
    falls back to the well-known dev key with a WARN log; PII in dev is
    throwaway.
    """
    raw = settings.ENCRYPTION_KEY.encode()

    # Preferred: base64-encoded 32 bytes.
    if len(raw) == 44:
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, base64.binascii.Error) as err:
            raise ValueError(
                "ENCRYPTION_KEY is 44 chars but not valid base64. Generate one with "
                '`python -c "import base64, secrets; '
                'print(base64.b64encode(secrets.token_bytes(32)).decode())"`'
            ) from err
        if len(decoded) == 32:
            return decoded

    # Also accept a raw 32-byte string for convenience.
    if len(raw) == 32:
        return raw

    if settings.ENVIRONMENT in _PROD_ENVS:
        raise RuntimeError(
            f"ENCRYPTION_KEY must be a 32-byte string or base64-encoded 32 bytes "
            f"(got len={len(raw)}). Refusing to use a padded / truncated key in "
            f"{settings.ENVIRONMENT} — it silently weakens AES-256. Generate one "
            'with `python -c "import base64, secrets; '
            'print(base64.b64encode(secrets.token_bytes(32)).decode())"`'
        )

    logger.warning(
        "ENCRYPTION_KEY is missing or wrong length (%d bytes); using dev fallback. "
        "Never do this outside ENVIRONMENT=dev.",
        len(raw),
    )
    return _DEV_FALLBACK_KEY


# Resolve once at import time so a misconfigured production process fails
# on boot instead of at the first encrypt() call (which may be minutes into
# the app's lifecycle). Cached so we don't re-validate on every operation.
_KEY: bytes = _resolve_key()


def _get_key() -> bytes:
    return _KEY


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
