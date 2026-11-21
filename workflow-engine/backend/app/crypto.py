"""Symmetric encryption for stored credentials (SMTP app passwords).

Key source, in order: ENCRYPTION_KEY in the environment/.env, else backend/.secret_key
(generated on first use, gitignored). Losing the key means stored passwords must be re-entered."""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import BACKEND_DIR, get_settings

KEY_FILE = BACKEND_DIR / ".secret_key"


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().encryption_key.strip()
    if not key:
        if KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            KEY_FILE.write_text(key)
    return Fernet(key.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("cannot decrypt stored credential (encryption key changed?) - re-enter the password") from e
