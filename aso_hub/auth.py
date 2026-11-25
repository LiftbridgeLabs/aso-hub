"""The owner's account: password, two-step codes, signed-in browsers, the first-run
setup code, and the pause after wrong guesses.
"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

from . import crypto, db, totp

MIN_PASSWORD, MAX_PASSWORD = 12, 200
SESSION_SECONDS = 30 * 24 * 3600
PENDING_SECONDS = 5 * 60  # between the password and the two-step code
RECOVERY_CODES = 10
# After this many wrong guesses (password, code or setup code), sign-in pauses.
PER_ADDRESS, EVERYONE, WINDOW = 10, 30, 15 * 60
FAILURES: dict[str, list[float]] = {}
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    """A problem to show the person signing in, in plain words."""


# Wrong guesses

def _recent(key: str, now: float) -> list[float]:
    return [t for t in FAILURES.get(key, []) if now - t < WINDOW]


def check_pause(address: str) -> None:
    now = time.monotonic()
    if len(_recent(address, now)) >= PER_ADDRESS or len(_recent("*", now)) >= EVERYONE:
        raise AuthError("Too many wrong tries. Wait a few minutes and try again.")


def wrong(address: str) -> None:
    now = time.monotonic()
    if len(FAILURES) > 1000:
        FAILURES.clear()
    for key in (address, "*"):
        FAILURES[key] = [*_recent(key, now), now]


def right(address: str) -> None:
    FAILURES.pop(address, None)


# Passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        found = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **_SCRYPT)
        return hmac.compare_digest(found, base64.b64decode(digest))
    except ValueError:
        return False


def check_new_password(password: str, again: str | None = None) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Use at most {MAX_PASSWORD} characters.")
    if again is not None and password != again:
        raise AuthError("The two passwords don't match.")


def account(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM account WHERE id = 1").fetchone()


def create_account(conn: sqlite3.Connection, password: str) -> None:
    check_new_password(password)
    with conn:
        made = conn.execute("INSERT OR IGNORE INTO account (id, password_hash, created_at) VALUES (1, ?, ?)",
                            (hash_password(password), db.now())).rowcount
    if not made:
        raise AuthError("This hub already has its account.")


def set_password(conn: sqlite3.Connection, password: str) -> None:
    """A new password, creating the account if there isn't one. Every browser signs in again."""
    check_new_password(password)
    if account(conn) is None:
        create_account(conn, password)
        return
    with conn:
        conn.execute("UPDATE account SET password_hash = ? WHERE id = 1", (hash_password(password),))
        conn.execute("DELETE FROM sessions")


def check_password(conn: sqlite3.Connection, password: str, address: str) -> None:
    check_pause(address)
    row = account(conn)
    if row is None or len(password) > MAX_PASSWORD or not password_matches(password, row["password_hash"]):
        wrong(address)
        raise AuthError("That password isn't right.")


def import_password_file(conn: sqlite3.Connection) -> bool:
    """Versions before 2.0 kept the password in a file: make it the account's, once."""
    name = os.environ.get("HUB_PASSWORD_FILE")
    if not name or account(conn) is not None:
        return False
    try:
        password = Path(name).read_text().strip()
        create_account(conn, password)
    except (OSError, UnicodeError, AuthError):
        return False
    return True


# First run

def setup_code(conn: sqlite3.Connection) -> str:
    """The code that proves whoever creates the account can read the server: SETUP_TOKEN,
    or one made on first start and printed in the log."""
    given = os.environ.get("SETUP_TOKEN", "").strip()
    if given:
        return given
    code = db.get_setting(conn, "setup_code")
    if not code:
        code = "-".join(secrets.token_hex(3) for _ in range(3))
        db.set_setting(conn, "setup_code", code)
    return code


def first_account(conn: sqlite3.Connection, code: str, password: str, again: str, address: str) -> None:
    check_pause(address)
    if account(conn) is not None:
        raise AuthError("This hub already has its account.")
    if not hmac.compare_digest(code.strip().encode(), setup_code(conn).encode()):
        wrong(address)
        raise AuthError("That setup code isn't right. It's printed in the hub's log.")
    check_new_password(password, again)
    create_account(conn, password)
    db.set_setting(conn, "setup_code", None)


# Signed-in browsers

def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(conn: sqlite3.Connection, pending: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + (PENDING_SECONDS if pending else SESSION_SECONDS)
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_fingerprint(token), int(pending), expires))
    return token


def session_ok(conn: sqlite3.Connection, token: str | None, pending: bool = False) -> bool:
    if not token:
        return False
    return conn.execute("SELECT 1 FROM sessions WHERE token_hash = ? AND pending = ? AND expires > ?",
                        (_fingerprint(token), int(pending), time.time())).fetchone() is not None


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_fingerprint(token),))


def end_other_sessions(conn: sqlite3.Connection, token: str | None) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash != ?", (_fingerprint(token or ""),))


# Two-step sign-in

def two_step_on(conn: sqlite3.Connection) -> bool:
    row = account(conn)
    return bool(row and row["totp_on"])


def begin_two_step(conn: sqlite3.Connection, database: Path, token: str | None) -> tuple[str, str]:
    """A secret for the authenticator app, and the link its QR code holds. Not on until
    confirmed. Remembers which browser asked: it just gave the password, so only it
    may see the secret again or confirm it."""
    row = account(conn)
    if row is None:
        raise AuthError("Create a password first.")
    if row["totp_on"]:
        raise AuthError("Two-step sign-in is already on.")
    secret = crypto.decrypt(database, row["totp_secret"]) if row["totp_secret"] else None
    if not secret:  # the same secret while setting up, so a reload doesn't spoil a scanned code
        secret = totp.new_secret()
        with conn:
            conn.execute("UPDATE account SET totp_secret = ? WHERE id = 1", (crypto.encrypt(database, secret),))
    db.set_setting(conn, "two_step_browser", _fingerprint(token or ""))
    return secret, totp.setup_uri(secret, account="owner")


def pending_two_step(conn: sqlite3.Connection, database: Path, token: str | None) -> tuple[str, str]:
    """The setup under way, for the browser that started it."""
    row = account(conn)
    mine = db.get_setting(conn, "two_step_browser") == _fingerprint(token or "")
    secret = crypto.decrypt(database, row["totp_secret"]) if row and row["totp_secret"] else None
    if not (mine and secret) or row["totp_on"]:
        raise AuthError("Start setting up two-step sign-in again.")
    return secret, totp.setup_uri(secret, account="owner")


def _recovery_code() -> str:
    text = base64.b32encode(secrets.token_bytes(7)).decode().lower()[:10]
    return f"{text[:5]}-{text[5:]}"


def _plain_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def new_recovery_codes(conn: sqlite3.Connection) -> list[str]:
    """Ten one-time codes, replacing any before. Only fingerprints are kept."""
    codes = [_recovery_code() for _ in range(RECOVERY_CODES)]
    with conn:
        conn.execute("DELETE FROM recovery_codes")
        conn.executemany("INSERT INTO recovery_codes (hash) VALUES (?)",
                         [(_fingerprint(_plain_recovery(c)),) for c in codes])
    return codes


def confirm_two_step(conn: sqlite3.Connection, database: Path, code: str, token: str | None) -> list[str]:
    secret, _ = pending_two_step(conn, database, token)
    step = totp.matching_step(secret, code, now=time.time())
    if step is None:
        raise AuthError("That code isn't right. Check that your phone's clock is set automatically.")
    with conn:
        conn.execute("UPDATE account SET totp_on = 1, totp_step = ? WHERE id = 1", (step,))
    db.set_setting(conn, "two_step_browser", None)
    end_other_sessions(conn, token)  # other browsers sign in again, now with a code
    return new_recovery_codes(conn)


def check_code(conn: sqlite3.Connection, database: Path, code: str, address: str) -> None:
    """The second step: a code from the app (each works once) or an unused recovery code."""
    check_pause(address)
    row = account(conn)
    secret = crypto.decrypt(database, row["totp_secret"]) if row and row["totp_secret"] else None
    step = totp.matching_step(secret, code, now=time.time(), after_step=row["totp_step"]) if secret else None
    with conn:
        if step is not None:
            if conn.execute("UPDATE account SET totp_step = ? WHERE id = 1 AND totp_step < ?", (step, step)).rowcount:
                return
        elif conn.execute("UPDATE recovery_codes SET used_at = ? WHERE hash = ? AND used_at IS NULL",
                          (db.now(), _fingerprint(_plain_recovery(code)))).rowcount:
            return
    wrong(address)
    raise AuthError("That code isn't right.")


def turn_off_two_step(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE account SET totp_on = 0, totp_secret = NULL, totp_step = 0 WHERE id = 1")
        conn.execute("DELETE FROM recovery_codes")
