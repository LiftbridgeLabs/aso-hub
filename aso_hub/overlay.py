"""The settings the hub runs with: what was saved in the app, laid over config.toml,
the environment and the secret files, plus the apps found in AsoBeast.

Anything not saved in the app keeps coming from where it did before, so a hub set
up with files carries on unchanged.
"""
import dataclasses
import sqlite3

from . import crypto, db, discover
from .settings import STORED, App, Settings

IDS = ("asc_issuer_id", "asc_key_id", "asc_vendor_number", "play_bucket", "asobeast_url", "asobeast_web_url")
# Settings field -> the name its secret is saved under.
SECRET_FIELDS = {"asc_key_path": "asc-key.p8", "google_key_path": "google-play.json",
                 "asobeast_token_file": "asobeast-token", "discord_webhook_file": "discord-webhook"}


def secrets(conn: sqlite3.Connection, settings: Settings) -> tuple[dict[str, str], set[str]]:
    """Secrets saved in the app, opened; and the names that can't be opened with today's key."""
    opened, broken = {}, set()
    for row in conn.execute("SELECT name, value FROM app_secrets"):
        value = crypto.decrypt(settings.database_path, row["value"])
        if value is None:
            broken.add(row["name"])
        else:
            opened[row["name"]] = value
    return opened, broken


def save_secret(conn: sqlite3.Connection, settings: Settings, name: str, value: str | None) -> None:
    """Save one secret encrypted, or with None remove it, so a secret file applies again."""
    with conn:
        if value is None:
            conn.execute("DELETE FROM app_secrets WHERE name = ?", (name,))
        else:
            conn.execute("INSERT OR REPLACE INTO app_secrets VALUES (?, ?, ?)",
                         (name, crypto.encrypt(settings.database_path, value), db.now()))


def apps(conn: sqlite3.Connection, settings: Settings) -> tuple[list[App], frozenset[str]]:
    """Apps listed by hand (the app's own first, then config.toml's) and the store ids left out."""
    mine = [App(**a, saved=True) for a in db.get_setting(conn, "apps", [])]
    taken = {(a.store, a.store_id) for a in mine}
    slugs = {a.slug for a in mine}
    from_file = [a for a in settings.apps if not a.auto and not a.saved
                 and (a.store, a.store_id) not in taken and a.slug not in slugs]
    ignore = settings.ignore_apps | frozenset(db.get_setting(conn, "hidden", []))
    return [a for a in mine + from_file if a.store_id not in ignore], ignore


def effective(settings: Settings, conn: sqlite3.Connection) -> Settings:
    """`settings` as read from files, with everything saved in the app applied."""
    changes: dict = {}
    ids = db.get_setting(conn, "ids", {})
    changes |= {key: ids[key] for key in IDS if ids.get(key)}
    opened, broken = secrets(conn, settings)
    changes |= {field: STORED / name for field, name in SECRET_FIELDS.items() if name in opened}
    listed, ignore = apps(conn, settings)
    changes |= {"apps": listed, "ignore_apps": ignore, "stored_secrets": opened, "broken_secrets": frozenset(broken)}
    rules = dict(settings.rules)
    for key, values in db.get_setting(conn, "rules", {}).items():
        rules[key] = rules.get(key, {}) | values
    changes["rules"] = rules
    for key in ("keywords", "crash_digest"):
        changes[key] = getattr(settings, key) | db.get_setting(conn, key, {})
    add = db.get_setting(conn, "add_apps")
    if add is not None:
        changes["add_apps"] = add
    return discover.with_apps(dataclasses.replace(settings, **changes), conn)
