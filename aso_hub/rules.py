"""The hub's own rules about the numbers.

Each rule returns what is true right now. An action opens the first pull its
rule fires, stays open (keeping its first-seen date) while it keeps firing,
and is resolved the first pull it doesn't. Thresholds come from config.toml.
"""
import datetime as dt
import sqlite3
import statistics
from dataclasses import dataclass

from . import asobeast, db, metrics, play
from .settings import App, Settings

# How long a new review stays on the list unless it gets a reply.
REVIEW_OPEN_DAYS = 7
# How long a rating-count change stays under "Worth knowing".
RATING_OPEN_DAYS = 3
# A keyword move this big shows next to a low-downloads note as a likely cause.
CAUSE_PLACES = 10
REPLY_WHERE = {"app_store": "App Store Connect (Ratings and Reviews)",
               "google_play": "Play Console (Ratings and reviews)"}


@dataclass(frozen=True)
class Finding:
    rule: str
    app_slug: str
    key: str
    title: str
    detail: str
    priority: str  # critical, high, medium or low, the same bands as AsoBeast
    # "do": something you can act on. "fyi": worth knowing, nothing to do about it directly.
    kind: str = "do"
    link: str | None = None  # where to act on it, if somewhere other than the hub
    link_text: str | None = None


def _fmt(n: float) -> str:
    return f"{n:.1f}".removesuffix(".0")


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _label(point: dict) -> str:
    return asobeast.position_label(point.get("position"), point.get("depth"))


def low_downloads(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    minimum = cfg.get("min_per_day", {}).get(app.slug)
    end = metrics.latest_sales_day(conn)
    if minimum is None or app.store != "app_store" or not end:
        return []
    days = cfg["days"]
    average = metrics.total(metrics.downloads(conn, app.apple_id, "US"), end, days) / days
    if average >= minimum:
        return []
    detail = (f"{app.label} averaged {_fmt(average)} first-time US downloads a day over the {days} days "
              f"to {metrics.short_date(end)} (alert below {_fmt(minimum)}).")
    drops = [m for m in asobeast.moves(conn, app, settings.keywords, "us", days) if m["change"] <= -CAUSE_PLACES]
    if drops:
        detail += " US keywords that fell in the same days: " + ", ".join(
            f"“{m['text']}” {_label(m['before'])} → {_label(m['after'])}" for m in drops[:3]) + "."
    return [Finding("low_downloads", app.slug, "us", "US downloads are low", detail, "medium", "fyi")]


def deletion_rate(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    end = metrics.latest_analytics_day(conn, app.apple_id, metrics.INSTALLS) if app.store == "app_store" else None
    if not end:
        return []
    days = cfg["days"]
    deleted = metrics.total(db.daily_totals(conn, app.apple_id, "deletions"), end, days)
    downloaded = metrics.total(metrics.downloads(conn, app.apple_id), end, days)
    if downloaded < cfg["min_downloads"] or deleted / downloaded <= cfg["max_rate"]:
        return []
    return [Finding("deletion_rate", app.slug, "all", "Many deletions",
                    f"{app.label}: {_fmt(deleted)} deletions against {_fmt(downloaded)} first-time downloads "
                    f"({deleted / downloaded:.0%}) over the {days} days to {metrics.short_date(end)} "
                    f"(alert above {cfg['max_rate']:.0%}).", "medium")]


def page_view_spike(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    end = metrics.latest_analytics_day(conn, app.apple_id) if app.store == "app_store" else None
    if not end:
        return []
    baseline = metrics.days_back(end, cfg["baseline_days"] + 1)[:-1]
    # Unique devices, so one person reloading the page (408 views from 30 devices) isn't a spike.
    views = db.daily_totals(conn, app.apple_id, "page_views", uniques=True)
    downloads = metrics.downloads(conn, app.apple_id)
    usual_views = sum(views.get(d, 0) for d in baseline) / len(baseline)
    usual_downloads = sum(downloads.get(d, 0) for d in baseline) / len(baseline)
    today_views, today_downloads = views.get(end, 0), downloads.get(end, 0)
    if today_views < max(cfg["min_views"], cfg["factor"] * usual_views):
        return []
    if usual_downloads and today_downloads >= cfg["downloads_factor"] * usual_downloads:
        return []
    return [Finding("page_view_spike", app.slug, end, "Page views spiked without downloads",
                    f"{app.label} had {_fmt(today_views)} product page viewers on {metrics.short_date(end)} "
                    f"(usually {_fmt(usual_views)} a day) but {_fmt(today_downloads)} first-time downloads "
                    f"(usually {_fmt(usual_downloads)}). Possibly a shared link that isn't converting.",
                    "medium", "fyi")]


def keyword_drop(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    """A shown keyword whose usual spot the week before (the middle of its checks) was in
    the top `from_top`, and that sits `places` or more below it on each of the last `days`
    checks. The usual spot, not the best day: some searches swing 40 places from one day
    to the next (a keyword that swings between #35 and #53), and one good day isn't a baseline."""
    series, _ = db.latest_snapshot(conn, "rankings", app.slug)
    hidden = asobeast.hidden_ids(conn, app, settings.keywords)
    days, places, from_top = cfg["days"], cfg["places"], cfg["from_top"]
    found = []
    for s in (series or {}).get("series", []):
        if s["country"] not in app.markets or s["keywordId"] in hidden:
            continue
        points = sorted(s["points"], key=lambda p: p["date"])
        window, before = points[-days:], points[-(days + 7):-days]
        # A full week before, so one early check can't be the usual spot of a new keyword.
        if len(window) < days or len(before) < 7 or not any(p.get("position") is not None for p in before):
            continue
        # Days it wasn't found count as just past the checked depth.
        usual = statistics.median_low(asobeast.ranked(p) for p in before)
        if usual <= from_top and all(asobeast.ranked(p) >= usual + places for p in window):
            now = ", ".join(_label(p) for p in window)
            found.append(Finding(
                "keyword_drop", app.slug, f"{s['country']}:{s['keywordId']}",
                f"“{s['text']}” dropped in {s['country'].upper()}",
                f"{app.label}: usually #{usual} the week before, {places}+ places lower on the last "
                f"{days} checks ({now}). Check whether a listing change or a competitor moved it.", "high"))
    return found


def new_review(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    """Reviews from the last week without a reply (to do), and new ratings (worth knowing)."""
    found = []
    since = (_today() - dt.timedelta(days=REVIEW_OPEN_DAYS)).isoformat()
    reviews, _ = db.latest_snapshot(conn, "reviews", app.slug)
    for r in (reviews or {}).get("reviews", []):
        if r["reviewedAt"][:10] < since or r.get("repliedAt"):
            continue
        text = (r.get("title") or "").strip()
        body = (r.get("text") or "").strip()
        quote = f"“{text}”" if text else ""
        if body:
            quote += (" " if quote else "") + (body[:160] + "…" if len(body) > 160 else body)
        found.append(Finding(
            "new_review", app.slug, r["reviewId"], f"New {r['score']}★ review",
            f"{app.label}, {metrics.short_date(r['reviewedAt'])}, version {r.get('version') or '?'}: {quote} "
            f"Reply in {REPLY_WHERE[app.store]}.", "medium" if r["score"] <= 3 else "low"))

    ratings, _ = db.latest_snapshot(conn, "ratings", app.slug)
    points = sorted((ratings or {}).get("points", []), key=lambda p: p["date"])
    recent = (_today() - dt.timedelta(days=RATING_OPEN_DAYS)).isoformat()
    for before, after in zip(points, points[1:], strict=False):
        if after["date"] >= recent and (after.get("ratingCount") or 0) > (before.get("ratingCount") or 0):
            found.append(Finding(
                "new_rating", app.slug, after["date"], "New rating",
                f"{app.label}: {before['ratingCount']} → {after['ratingCount']} ratings, average "
                f"{before['ratingAvg']:.2f} → {after['ratingAvg']:.2f} ({metrics.short_date(after['date'])}).",
                "low", "fyi"))
    return found


def play_crash(conn: sqlite3.Connection, app: App, cfg: dict, settings: Settings) -> list[Finding]:
    """A Google Play crash or ANR reported in the last `days` days."""
    if app.store != "google_play":
        return []
    since = (_today() - dt.timedelta(days=cfg.get("days", 7))).isoformat()
    body, _ = db.latest_snapshot(conn, "play_issues", app.slug)
    found = []
    for issue in (body or {}).get("issues", []):
        last = issue.get("lastErrorReportTime", "")
        if last[:10] < since:
            continue
        users = int(issue.get("distinctUsers") or 0)
        reports = int(issue.get("errorReportCount") or 0)
        version = (issue.get("lastAppVersion") or {}).get("versionCode", "?")
        what = " in ".join(p for p in (issue.get("location"), issue.get("cause")) if p)
        found.append(Finding(
            "play_crash", app.slug, issue["name"].rsplit("/", 1)[-1], f"{play.issue_label(issue)} on Android",
            f"{app.label}: {what or 'no details'}. {reports} report{'' if reports == 1 else 's'} from "
            f"{users} user{'' if users == 1 else 's'} in {play.ISSUE_DAYS} days, version code {version}, "
            f"last on {metrics.short_date(last[:10])}.",
            "high" if users >= cfg.get("many_users", 3) else "medium",
            link=issue.get("issueUri"), link_text="Open it in Play Console"))
    return found


RULES = {"low_downloads": low_downloads, "deletion_rate": deletion_rate, "page_view_spike": page_view_spike,
         "keyword_drop": keyword_drop, "new_review": new_review, "play_crash": play_crash}


def findings(settings: Settings, conn: sqlite3.Connection) -> list[Finding]:
    found = []
    for name, rule in RULES.items():
        cfg = settings.rules.get(name, {})
        if cfg.get("enabled"):
            for app in settings.apps:
                found += rule(conn, app, cfg, settings)
    return found


def evaluate(settings: Settings, conn: sqlite3.Connection) -> list[Finding]:
    """Open, refresh and resolve the hub's actions to match what fires now."""
    now = db.now()
    current = findings(settings, conn)
    with conn:
        for f in current:
            conn.execute(
                "INSERT INTO hub_actions (rule, app_slug, key, title, detail, priority, kind, first_seen, last_seen, "
                "link, link_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rule, app_slug, key) DO UPDATE SET title = excluded.title, detail = excluded.detail, "
                "priority = excluded.priority, kind = excluded.kind, last_seen = excluded.last_seen, "
                "link = excluded.link, link_text = excluded.link_text, "
                # A resolved action that fires again starts over.
                "first_seen = CASE WHEN resolved_at IS NULL THEN first_seen ELSE excluded.first_seen END, "
                "resolved_at = NULL",
                (f.rule, f.app_slug, f.key, f.title, f.detail, f.priority, f.kind, now, now, f.link, f.link_text))
        firing = {(f.rule, f.app_slug, f.key) for f in current}
        for row in conn.execute("SELECT rule, app_slug, key FROM hub_actions WHERE resolved_at IS NULL").fetchall():
            if (row["rule"], row["app_slug"], row["key"]) not in firing:
                conn.execute("UPDATE hub_actions SET resolved_at = ? WHERE rule = ? AND app_slug = ? AND key = ?",
                             (now, row["rule"], row["app_slug"], row["key"]))
    return current
