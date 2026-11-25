"""Apps picked up from AsoBeast, so tracking an app there is all it takes.

config.toml still wins: an app listed there keeps its own name, address and
markets. Apps only AsoBeast knows about are saved by each AsoBeast pull and
added to the list here.
"""
import dataclasses
import re
import sqlite3

from . import db
from .settings import App, Settings

STORES = {"APP_STORE": "app_store", "GOOGLE_PLAY": "google_play"}
SUFFIX = {"app_store": "ios", "google_play": "android"}
# Ids go into Apple's and Google's web addresses, so anything oddly shaped is left out.
STORE_IDS = {"app_store": re.compile(r"\d{6,15}"),
             "google_play": re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")}
COUNTRY = re.compile(r"[a-z]{2}")


def with_apps(settings: Settings, conn: sqlite3.Connection) -> Settings:
    """The settings with config.toml's apps, then the ones last found in AsoBeast."""
    configured = [a for a in settings.apps if not a.auto]
    if not settings.add_apps:
        return dataclasses.replace(settings, apps=configured)
    body, _ = db.latest_snapshot(conn, "discovered", "all")
    listed = {(a.store, a.store_id) for a in configured}
    slugs = {a.slug for a in configured}
    found = [App(**saved, auto=True) for saved in (body or {}).get("apps", [])]
    found = [a for a in found if (a.store, a.store_id) not in listed and a.slug not in slugs
             and a.store_id not in settings.ignore_apps]
    return dataclasses.replace(settings, apps=configured + found)


def short_name(title: str) -> str:
    """A store title without its tagline: "Notes: Quick Memos" -> "Notes"."""
    return re.split(r"\s*[:|]\s*|\s+[-–—]\s+", title.strip(), maxsplit=1)[0].strip() or title.strip()


def describe(details: list[dict], settings: Settings, previous: list[dict]) -> list[dict]:
    """The apps in AsoBeast that config.toml doesn't list, ready to save.

    `details` are AsoBeast's app answers, each with "markets" added. An app keeps
    the address it was first given, so its page and to-dos survive a new title.
    """
    configured = [a for a in settings.apps if not a.auto]
    listed = {(a.store, a.store_id) for a in configured}
    kept = {(p["store"], p.get("apple_id") or p.get("google_package")): p["slug"] for p in previous}
    wanted = []
    for d in details:
        store, sid = STORES.get(d.get("store")), str(d.get("storeAppId") or "")
        if (not store or not STORE_IDS[store].fullmatch(sid) or (store, sid) in listed
                or sid in settings.ignore_apps or d.get("delistedAt")):
            continue
        wanted.append((store, sid, str(d.get("name") or sid), d.get("markets") or []))
    shorts = [(store, short_name(title)) for store, _, title, _ in wanted]
    used = {a.slug for a in configured} | set(kept.values())
    out = []
    for store, sid, title, markets in sorted(wanted, key=lambda w: (w[2].lower(), w[0])):
        # Two apps in one store that shorten to the same name keep their full titles.
        name = title if shorts.count((store, short_name(title))) > 1 else short_name(title)
        slug = kept.get((store, sid))
        if not slug or slug in {a.slug for a in configured}:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "app"
            slug, n = f"{base}-{SUFFIX[store]}", 2
            while slug in used:
                slug, n = f"{base}-{n}-{SUFFIX[store]}", n + 1
        used.add(slug)
        out.append({"slug": slug, "name": name, "store": store,
                    "markets": [m for m in markets if COUNTRY.fullmatch(m)] or ["us"],
                    "apple_id" if store == "app_store" else "google_package": sid})
    return out
