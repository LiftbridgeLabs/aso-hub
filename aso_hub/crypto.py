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
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _key_text(database_path: Path) -> str:
    given = os.environ.get("SECRET_KEY", "").strip()
    if given:
        return given
    path = database_path.parent / "secret.key"
    if not path.exists():
        _make_key(path)
    text = path.read_text().strip()
    if not text:
        raise RuntimeError(f"{path} is empty. Delete it to make a new key, then enter your saved keys again.")
    return text


def _make_key(path: Path) -> None:
    """Written whole under another name, then given its name in one step, and never over
    an existing key: two starts at once end up reading the same complete one."""
    fd, draft = tempfile.mkstemp(dir=path.parent, prefix=".secret-")  # private to the hub's user
    try:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
        try:
            os.link(draft, path)
        except FileExistsError:
            pass
        except OSError:  # a disk without hard links
            if not path.exists():
                os.replace(draft, path)
    finally:
        Path(draft).unlink(missing_ok=True)


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
