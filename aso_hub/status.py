"""What the Settings page shows: the version, the sign-in, what's set up, the apps and rules.

Nothing here can change anything, and no key, token, id or webhook is ever
returned: only whether each one is there.
"""
import os
from pathlib import Path

from . import guard
from .settings import Settings

VERSION = os.environ.get("HUB_BUILD_VERSION") or "dev"

RULES = {
    "low_downloads": "Downloads are low",
    "deletion_rate": "Many deletions",
    "page_view_spike": "Page views spike without downloads",
    "keyword_drop": "A keyword drops",
    "new_review": "New rating or review",
    "play_crash": "Google Play crash or freeze",
}
THRESHOLDS = {
    "days": "days looked at", "min_per_day": "lowest downloads a day", "max_rate": "most deletions per download",
    "min_downloads": "downloads needed", "baseline_days": "days compared with", "factor": "times the usual views",
    "min_views": "views needed", "downloads_factor": "downloads below this many times usual",
    "places": "places lost", "from_top": "only keywords in the top", "many_users": "people for high priority",
}


def _file(path: Path | None) -> tuple[bool, str]:
    """Whether a secret file is there and holds something, without keeping what it holds."""
    if path is None:
        return False, "Not set up"
    try:
        found = bool(path.read_text().strip())
    except FileNotFoundError:
        return False, "Not saved"
    except (OSError, UnicodeError):
        return False, "Saved, but the hub can't read it"
    return found, "Saved" if found else "The file is empty"


def _value(value: str | None) -> tuple[bool, str]:
    return bool(value), "Set" if value else "Not set"


def sign_ins() -> list[str]:
    """Every sign-in protecting the hub, in plain words."""
    out = []
    if os.environ.get("CF_ACCESS_AUD"):
        out.append("Cloudflare Access: every request must carry Cloudflare's signed sign-in.")
    try:
        secret = guard.password()
    except (OSError, UnicodeError):
        secret = None
    if secret:
        out.append("The hub's password.")
    if os.environ.get("ASO_HUB_BEHIND_SIGN_IN", "").strip().lower() in {"1", "true", "yes"}:
        out.append("Another sign-in service, as declared by ASO_HUB_BEHIND_SIGN_IN. The hub can't check it.")
    return out


def sources(settings: Settings) -> list[dict]:
    """Each connection and the pieces it needs: (name, ready, words, how to set it)."""
    groups = [
        ("App Store Connect", [
            ("Issuer id", _value(settings.asc_issuer_id), "Add ASC_ISSUER_ID to .env"),
            ("Key id", _value(settings.asc_key_id), "Add ASC_KEY_ID to .env"),
            ("Vendor number", _value(settings.asc_vendor_number), "Add ASC_VENDOR_NUMBER to .env"),
            ("Key file", _file(settings.asc_key_path), "Put asc-key.p8 in secrets/")]),
        ("Google Play", [
            ("Service account key", _file(settings.google_key_path), "Put google-play.json in secrets/"),
            ("Reports bucket", _value(settings.play_bucket), "Add PLAY_BUCKET to .env")]),
        ("AsoBeast", [
            ("Read-only token", _file(settings.asobeast_token_file), "Run set-secret.sh asobeast-token")]),
        ("Discord", [
            ("New to-dos", _file(settings.discord_webhook_file), "Run set-secret.sh discord-webhook")]),
    ]
    digest = [(name, _file(settings.alerts_webhook_file(name)),
               f"Run set-secret.sh {settings.alerts_webhook_file(name).name}")
              for name in settings.crash_digest.get("apps", []) if settings.alerts_webhook_dir]
    if digest:
        digest.append(("When a digest fails", _file(settings.alerts_webhook_file("admin")),
                       "Run set-secret.sh alerts-admin"))
        groups.append(("Weekly crash digest", digest))
    return [{"name": name, "items": [{"name": n, "ok": ok, "state": words, "how": how}
                                     for n, (ok, words), how in items]} for name, items in groups]


def _plain(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_plain(v)}" for k, v in value.items()) or "none"
    if isinstance(value, list):
        return ", ".join(map(str, value)) or "none"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def rules(settings: Settings) -> list[dict]:
    return [{"name": RULES.get(key, key), "key": key, "on": cfg.get("enabled", False),
             "values": [f"{THRESHOLDS.get(k, k)}: {_plain(v)}" for k, v in cfg.items() if k != "enabled"]}
            for key, cfg in settings.rules.items()]


def page(settings: Settings) -> dict:
    return {
        "version": VERSION,
        "sign_ins": sign_ins(),
        "hosts": sorted(guard.allowed_hosts()),
        "connections": sources(settings),
        "rules": rules(settings),
        "add_apps": settings.add_apps,
        "ignored": sorted(settings.ignore_apps),
        "digest_apps": settings.crash_digest.get("apps", []),
        "config_file": os.environ.get("HUB_CONFIG") or "config.toml",
    }
