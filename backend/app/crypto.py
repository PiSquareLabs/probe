"""Symmetric encryption for credentials held at rest.

Credentials entered in the UI are written to disk encrypted with a Fernet key
supplied through ``PROBE_SECRET_KEY``. Plaintext never leaves this module except
on the way to Jira, Elasticsearch or Kibana.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .errors import ConfigurationError

_PREFIX = "enc::"


def _derive_key(secret: str) -> bytes:
    """Accept either a real Fernet key or any passphrase.

    A 32-byte url-safe base64 value is used as-is; anything else is stretched
    with SHA-256 so that a human-typed secret still yields a valid key.
    """
    candidate = secret.encode("utf-8")
    try:
        if len(base64.urlsafe_b64decode(candidate)) == 32:
            return candidate
    except Exception:  # noqa: BLE001 - not a base64 Fernet key, fall through
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())


class SecretBox:
    def __init__(self, secret: str):
        if not secret:
            raise ConfigurationError(
                "PROBE_SECRET_KEY is not set, so credentials cannot be stored safely.",
                hint=(
                    'Generate one with: python -c "from cryptography.fernet import '
                    'Fernet; print(Fernet.generate_key().decode())"'
                ),
            )
        self._fernet = Fernet(_derive_key(secret))

    def encrypt(self, plaintext: str) -> str:
        if plaintext == "":
            return ""
        return _PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith(_PREFIX):
            # Written before encryption was enabled, or injected by hand.
            return value
        try:
            return self._fernet.decrypt(value[len(_PREFIX) :].encode("utf-8")).decode(
                "utf-8"
            )
        except InvalidToken as exc:
            raise ConfigurationError(
                "Stored credentials cannot be decrypted with the current "
                "PROBE_SECRET_KEY.",
                hint="Re-enter the credentials, or restore the original key.",
            ) from exc


def mask(value: str, *, keep_start: int = 6, keep_end: int = 4) -> str:
    """Render a secret for display: ``ATATT3…4e2a``."""
    if not value:
        return ""
    if len(value) <= keep_start + keep_end:
        return "…" * len(value)
    return f"{value[:keep_start]}…{value[-keep_end:]}"
