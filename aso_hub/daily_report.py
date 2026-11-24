"""The numbers for the 9:00 AM Discord report (aso-reports/report.py), from the hub.

Prints the same JSON shape the report used to get from ASO Atlas, so its
formatting and posting stay as they were. `source` is "hub" so the weekly rank
comparison never mixes these with the older ASO Atlas snapshots.
"""
import sqlite3

from . import asobeast, db, metrics, play_reports
from .settings import App, Settings

DAYS = 7


def _series(values: dict[str, float], end: str, first_day: str | None) -> list[dict]:
    """The last DAYS days ending on `end`, zero-filled, starting no earlier than the
    app's first day with any numbers (a new app has no week to average yet)."""
    days = [d for d in metrics.days_back(end, DAYS) if first_day and d >= first_day]
    return [{"date": d, "value": _number(values.get(d, 0))} for d in days]


def _number(value: float) -> int | float:
    """Whole counts as 8, not 8.0; money keeps its cents."""
    value = round(value, 2)
    return int(value) if float(value).is_integer() else value


def _ranks(conn: sqlite3.Connection, app: App, prefs: dict) -> list[dict]:
    out = []
    for country in app.markets:
        keywords, _ = db.latest_snapshot(conn, "keywords", f"{app.slug}:{country}")
        for k in keywords or []:
            if not asobeast.is_hidden(k, prefs):
                out.append({"term": k["text"], "country": country, "position": k.get("latestPosition")})
    return out


def _app_store(conn: sqlite3.Connection, app: App, end: str | None) -> tuple[list, list]:
    downloads = metrics.downloads(conn, app.apple_id)
    proceeds = db.daily_totals(conn, app.apple_id, "proceeds_usd")
    if not end or not downloads:
        return [], []
    first = min(downloads)
    return _series(downloads, end, first), _series(proceeds, end, first)


def _google_play(conn: sqlite3.Connection, app: App) -> list:
    installs = play_reports.daily(conn, app, "user_installs")
    end = play_reports.latest_day(conn, app, "user_installs")
    return _series(installs, end, min(installs)) if end and installs else []


def _trend(value: dict | None) -> dict | None:
    if not value or value.get("current") is None:
        return None
    return {"current": value["current"], "delta7d": value.get("delta7d")}


def _visibility(conn: sqlite3.Connection, cfg: Settings) -> tuple[dict, list]:
    """AsoBeast's visibility and audit score per hub app, and per linked group
    (one app on both stores), each with its 7-day change."""
    portfolio, _ = db.latest_snapshot(conn, "portfolio", "all")
    insights, _ = db.latest_snapshot(conn, "portfolio_insights", "all")
    ids, _ = db.latest_snapshot(conn, "ids", "all")
    slug_of = {aid: slug for slug, aid in (ids or {}).items()}
    audits = {i.get("appId"): i.get("audit") for i in (insights or {}).get("apps", [])}
    per_app = {}
    for item in (portfolio or {}).get("apps", []):
        slug = slug_of.get(item.get("id"))
        if slug and cfg.app(slug):
            per_app[slug] = {"visibility": _trend(item.get("visibility")),
                             "audit": _trend(audits.get(item["id"]))}
    groups = []
    for group in (portfolio or {}).get("groups", []):
        member_ids = group.get("memberAppIds", [])
        members = [cfg.app(slug_of[m]) if m in slug_of else None for m in member_ids]
        if len(members) < 2 or None in members:
            continue  # a listing the hub doesn't (or no longer) know: its name would be a guess
        names = {m.name for m in members}
        if len(names) == 1 and _trend(group.get("visibility")):
            groups.append({"name": names.pop(), "visibility": _trend(group["visibility"])})
    return per_app, groups


def snapshot(conn: sqlite3.Connection, cfg: Settings) -> dict:
    end = metrics.latest_sales_day(conn)
    per_app, groups = _visibility(conn, cfg)
    apps = []
    for app in cfg.apps:
        if app.store == "app_store":
            downloads, proceeds = _app_store(conn, app, end)
        else:
            downloads, proceeds = _google_play(conn, app), []
        apps.append({"id": app.slug, "name": app.name,
                     "platform": "ios" if app.store == "app_store" else "android",
                     "downloads": downloads, "proceeds": proceeds,
                     "ranks": _ranks(conn, app, cfg.keywords)} | per_app.get(app.slug, {}))
    return {"source": "hub", "apps": apps, "groups": groups}
