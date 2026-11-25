"""What the Settings pages show about the setup: the version, the sign-in, what's
connected, the apps and rules.

No key, token or webhook is ever returned: only whether each one is there.
"""
import dataclasses
import os
import sqlite3
from pathlib import Path

from . import auth, guard, oidc
from .settings import STORED, Settings

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


def secret_state(settings: Settings, name: str, path: Path | None) -> dict:
    """Whether one secret is there and where it comes from, without returning it."""
    if name in settings.broken_secrets:
        # A file on the server may be standing in: say what the hub is actually using.
        stand_in = secret_state(dataclasses.replace(settings, broken_secrets=frozenset()), name, path)
        state = ("Using the file on the server; the one saved in the hub can't be opened" if stand_in["ok"]
                 else "Can't be opened: enter it again")
        return {"ok": stand_in["ok"], "state": state, "saved": True}
    if path is not None and path.parent == STORED:
        return {"ok": True, "state": "Saved in the hub", "saved": True}
    try:
        found = bool(settings.read_secret(path))
    except (OSError, UnicodeError):
        return {"ok": False, "state": "A file the hub can't read", "saved": False}
    return {"ok": found, "state": "From a file on the server" if found else "Not saved", "saved": False}


def secret_states(settings: Settings) -> dict[str, dict]:
    paths = {"asc-key.p8": settings.asc_key_path, "google-play.json": settings.google_key_path,
             "asobeast-token": settings.asobeast_token_file, "discord-webhook": settings.discord_webhook_file}
    return {name: secret_state(settings, name, path) for name, path in paths.items()}


def webhooks(settings: Settings) -> list[dict]:
    """The new to-dos webhook, then the weekly crash digest's."""
    out = [{"name": "discord-webhook", "label": "New to-dos"}
           | secret_state(settings, "discord-webhook", settings.discord_webhook_file)]
    names = settings.crash_digest.get("apps", [])
    for label, app in [*((f"Crash digest: {n}", n) for n in names),
                       *([("When a crash digest fails", "admin")] if names else [])]:
        path = settings.alerts_webhook_file(app)
        name = path.name if path else "alerts-" + settings.webhook_slug(app)
        out.append({"name": name, "label": label} | secret_state(settings, name, path))
    return out


def sign_ins(conn: sqlite3.Connection) -> list[str]:
    """Every way in, in plain words."""
    out = []
    if guard.cloudflare_access():
        out.append("Cloudflare Access: every request must carry Cloudflare's signed sign-in.")
    if auth.account(conn) is not None:
        step = "with two-step codes" if auth.two_step_on(conn) else "without two-step codes"
        out.append(f"The hub's password, {step}.")
    names = [p["name"] for p in oidc.providers(conn, enabled_only=True)]
    if names:
        out.append(f"Single sign-on: {', '.join(names)}.")
    return out


def sources(settings: Settings) -> list[dict]:
    """Each connection and whether it has what it needs."""
    states = secret_states(settings)
    asc_ready = bool(settings.asc_issuer_id and settings.asc_key_id and settings.asc_vendor_number
                     and states["asc-key.p8"]["ok"])
    rows = [("App Store Connect", asc_ready), ("Google Play", states["google-play.json"]["ok"]),
            ("AsoBeast", states["asobeast-token"]["ok"]), ("Discord to-dos", states["discord-webhook"]["ok"])]
    return [{"name": name, "ok": ok, "state": "Ready" if ok else "Not set up"} for name, ok in rows]
