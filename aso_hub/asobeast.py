"""AsoBeast: rankings, competitors, keyword coverage, actions, ratings and reviews.

The hub reads AsoBeast's API inside the server (http://asobeast-api:4000) with a
read-only token, and saves each answer as a daily snapshot. Built against
AsoBeast 1.13.0; test the hub after changing ASOBEAST_VERSION.
"""
import datetime as dt
import sqlite3

import httpx

from . import db
from .settings import App, Settings

STORES = {"app_store": "APP_STORE", "google_play": "GOOGLE_PLAY"}
RANKING_DAYS = 30
REVIEW_DAYS = 60
# AsoBeast's own words for how a keyword got tracked; MANUAL means you added it.
AUTO_SOURCES = {"TITLE", "SUBTITLE", "KEYWORD_FIELD"}


class AsoBeastError(Exception):
    """A problem worth showing on the page, in plain words."""


class Client:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=base_url, timeout=60, transport=transport,
                                  headers={"Authorization": f"Bearer {token}"})

    def get(self, path: str, params: dict | None = None):
        r = self._http.get(path, params=params)
        if r.status_code == 401:
            raise AsoBeastError("AsoBeast refused the hub's token (revoked or expired). Make a new read-only one.")
        if r.status_code >= 400:
            raise AsoBeastError(f"AsoBeast answered {r.status_code} for {path}.")
        return r.json()


def store_id(app: App) -> str | None:
    return app.apple_id if app.store == "app_store" else app.google_package


def match_apps(client: Client, apps: list[App]) -> dict[str, str]:
    """Hub app slug -> AsoBeast app id, matched by store and store id."""
    wanted = {(STORES[a.store], store_id(a)): a.slug for a in apps}
    found = {}
    for item in client.get("/apps"):
        detail = client.get(f"/apps/{item['id']}")
        slug = wanted.get((detail["store"], detail.get("storeAppId")))
        if slug:
            found[slug] = item["id"]
    return found


def pull(conn: sqlite3.Connection, settings: Settings, client: Client | None = None,
         today: dt.date | None = None) -> str:
    if client is None:
        token = settings.read_secret(settings.asobeast_token_file)
        if not token:
            raise AsoBeastError("The AsoBeast token isn't set up (ASOBEAST_TOKEN_FILE).")
        client = Client(settings.asobeast_url, token)
    today = today or dt.datetime.now(dt.UTC).date()  # AsoBeast's days are UTC dates
    ids = match_apps(client, settings.apps)

    # Fetch everything, then save it in one go, so a failed pull leaves the last good day intact.
    snapshots: list[tuple[str, str, object]] = [
        ("ids", "all", ids), ("actions", "all", client.get("/actions")),
        # Visibility, audit score and their 7-day changes, per app and per linked group
        # (Lumen's Apple and Android listings), for the Monday post.
        ("portfolio", "all", client.get("/portfolio")),
        ("portfolio_insights", "all", client.get("/portfolio/insights")),
    ]
    rankings_from = (today - dt.timedelta(days=RANKING_DAYS)).isoformat()
    reviews_from = (today - dt.timedelta(days=REVIEW_DAYS)).isoformat()
    for app in settings.apps:
        aid = ids.get(app.slug)
        if not aid:
            continue
        snapshots += [
            ("app", app.slug, client.get(f"/apps/{aid}")),
            ("compare", app.slug, client.get(f"/apps/{aid}/keywords/compare")),
            ("rankings", app.slug, client.get(f"/apps/{aid}/rankings",
                                              {"from": rankings_from, "to": today.isoformat()})),
            ("reviews", app.slug, client.get(f"/apps/{aid}/reviews", {"from": reviews_from, "limit": 100})),
            ("ratings", app.slug, client.get(f"/apps/{aid}/ratings-history",
                                             {"from": reviews_from, "to": today.isoformat()})),
        ]
        for country in app.markets:
            snapshots += [
                ("keywords", f"{app.slug}:{country}", client.get(f"/apps/{aid}/keywords", {"country": country})),
                ("audit", f"{app.slug}:{country}", client.get(f"/apps/{aid}/metadata/audit", {"country": country})),
            ]
            if app.store == "app_store":
                # Every keyword field a market's search reads (the US store also reads Spanish (Mexico)).
                snapshots.append(("market", f"{app.slug}:{country}",
                                  client.get(f"/apps/{aid}/keyword-fields/market", {"country": country})))
    with conn:
        for kind, key, body in snapshots:
            db.save_snapshot(conn, today.isoformat(), kind, key, body)
    missing = [a.label for a in settings.apps if a.slug not in ids]
    message = f"read {len(ids)} apps"
    return message + (f"; not tracked in AsoBeast: {', '.join(missing)}" if missing else "")


def is_hidden(keyword: dict, prefs: dict) -> bool:
    """Single words AsoBeast added from the listing itself (it re-adds them every
    day), unless config.toml keeps them. Keywords you added always show."""
    if not prefs.get("hide_single_words") or keyword.get("source") not in AUTO_SOURCES:
        return False
    text = keyword["text"]
    return " " not in text.strip() and text not in prefs.get("keep_single_words", [])


def hidden_ids(conn: sqlite3.Connection, app: App, prefs: dict) -> set[str]:
    out = set()
    for country in app.markets:
        keywords, _ = db.latest_snapshot(conn, "keywords", f"{app.slug}:{country}")
        out |= {k["keywordId"] for k in keywords or [] if is_hidden(k, prefs)}
    return out


def ranked(point: dict) -> int:
    """A position for comparing: not found within the checked depth counts as just past it."""
    return point["position"] if point.get("position") is not None else (point.get("depth") or 200) + 1


def position_label(position: int | None, depth: int | None) -> str:
    if position is not None:
        return f"#{position}"
    return f"not in top {depth}" if depth else "not checked yet"


def moves(conn: sqlite3.Connection, app: App, prefs: dict, country: str, days: int = 7) -> list[dict]:
    """How each shown keyword's position changed over the last `days` checked days
    in one market, biggest moves first. `change` > 0 means it moved up."""
    series, _ = db.latest_snapshot(conn, "rankings", app.slug)
    hidden = hidden_ids(conn, app, prefs)
    out = []
    for s in (series or {}).get("series", []):
        if s["country"] != country or s["keywordId"] in hidden or not s["points"]:
            continue
        points = sorted(s["points"], key=lambda p: p["date"])
        latest = points[-1]
        cutoff = (dt.date.fromisoformat(latest["date"]) - dt.timedelta(days=days)).isoformat()
        earlier = next((p for p in reversed(points) if p["date"] <= cutoff), points[0])
        if earlier is latest or (earlier.get("position") is None and latest.get("position") is None):
            continue
        out.append({"keywordId": s["keywordId"], "text": s["text"], "country": country,
                    "before": earlier, "after": latest, "change": ranked(earlier) - ranked(latest)})
    return sorted(out, key=lambda m: -abs(m["change"]))
