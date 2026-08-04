"""Tests for AES-256 key resolution + validation.

The previous behaviour silently padded a too-short ENCRYPTION_KEY with
null bytes, downgrading AES-256 to weaker crypto without a warning. This
locks in the new behaviour: prod fails hard on a bad key; dev falls back
loudly to a well-known dev key.
"""

import base64
import importlib
import secrets

import pytest


def _fresh_module(monkeypatch, *, env: str, key: str):
    """Reload app.utils.encryption after setting the two env vars it reads.

    `_KEY` is computed at import time (fail-fast on boot), so a test that
    wants to change the config MUST reload the module — otherwise it just
    sees the value cached the first time any other test imported it.
    """
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    # Also poke the Settings singleton so `get_settings()` re-reads env.
    import app.config

    app.config.get_settings.cache_clear()

    import app.utils.encryption

    return importlib.reload(app.utils.encryption)


def test_raw_32_byte_key_accepted(monkeypatch):
    key = "a" * 32
    mod = _fresh_module(monkeypatch, env="production", key=key)
    assert mod._get_key() == key.encode()


def test_base64_encoded_key_accepted(monkeypatch):
    raw = secrets.token_bytes(32)
    b64 = base64.b64encode(raw).decode()
    assert len(b64) == 44
    mod = _fresh_module(monkeypatch, env="production", key=b64)
    assert mod._get_key() == raw


def test_short_key_rejected_in_production(monkeypatch):
    """The whole point — prod must not silently pad a weak key."""
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY must be"):
        _fresh_module(monkeypatch, env="production", key="short")


def test_short_key_rejected_in_staging(monkeypatch):
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY must be"):
        _fresh_module(monkeypatch, env="staging", key="also-too-short")


def test_bad_base64_rejected(monkeypatch):
    """44 chars but not valid base64 → explicit ValueError, not a padded
    fallback. Common failure mode: someone hand-typed a placeholder."""
    with pytest.raises(ValueError, match="not valid base64"):
        _fresh_module(monkeypatch, env="production", key="!" * 44)


def test_dev_falls_back_to_wellknown_key(monkeypatch):
    """Local dev workflow: unset / wrong key → boot with the dev fallback
    (with a WARN log) so contributors don't need to provision a real key."""
    mod = _fresh_module(monkeypatch, env="dev", key="")
    assert mod._get_key() == mod._DEV_FALLBACK_KEY


def test_encrypt_decrypt_roundtrip_with_validated_key(monkeypatch):
    """Sanity check: after validation, the resolved key actually works for
    AES-GCM (rules out an off-by-one or wrong-byte-order regression)."""
    raw = secrets.token_bytes(32)
    b64 = base64.b64encode(raw).decode()
    mod = _fresh_module(monkeypatch, env="production", key=b64)
    ciphertext = mod.encrypt("hello aadhaar 123456789012")
    assert mod.decrypt(ciphertext) == "hello aadhaar 123456789012"
