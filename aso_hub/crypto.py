"""Encryption for what the hub has to store and use again: keys, tokens, webhooks,
the two-step secret and single sign-on client secrets.

The encryption key comes from SECRET_KEY, or from a file the hub makes next to
its database the first time. A database copied without that key keeps its secrets
shut. Someone who can read both can open them, as they could any key file.
"""
import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _key_text(database_path: Path) -> str:
    given = os.environ.get("SECRET_KEY", "").strip()
    if given:
        return given
    path = database_path.parent / "secret.key"
    try:
        # Created already private, and only if it isn't there: two starts at once keep one key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
    text = path.read_text().strip()
    if not text:
        raise RuntimeError(f"{path} is empty. Delete it to make a new key, then enter your saved keys again.")
    return text


def _fernet(database_path: Path) -> Fernet:
    digest = hashlib.sha256(f"aso-hub:{_key_text(database_path)}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(database_path: Path, value: str) -> str:
    return _fernet(database_path).encrypt(value.encode()).decode()


def decrypt(database_path: Path, value: str) -> str | None:
    """None when the value was saved with another key (a changed SECRET_KEY, a restored backup)."""
    try:
        return _fernet(database_path).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None
