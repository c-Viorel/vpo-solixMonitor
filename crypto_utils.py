"""
Credential encryption helpers.

Credentials (Anker email + password) are stored AES-encrypted (Fernet/AES-128-CBC)
in the settings table.  The encryption key lives in data/.enc_key (600 perms).
Never commit that file to version control.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet

from config import Config


def _get_fernet() -> Fernet:
    key_path = Path(Config.ENCRYPTION_KEY_FILE)
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Return a URL-safe base64-encoded encrypted string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt()."""
    return _get_fernet().decrypt(token.encode()).decode()
