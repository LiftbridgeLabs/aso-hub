"""The hub's pages. Read-only: they show what the last pull saved."""
import datetime as dt
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asobeast, charts, db, discover, keywords, metrics, play, play_reports, settings, status
from .access import AccessCheck
from .guard import Guard

HERE = Path(__file__).resolve().parent
SOURCE_NAMES = {"asc": "App Store Connect", "asobeast": "AsoBeast", "play": "Google Play"}
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AccessCheck)
app.add_middleware(Guard)  # added last, so it runs first and its headers are on every answer
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["short_date"] = metrics.short_date
templates.env.filters["month"] = lambda ym: dt.date.fromisoformat(f"{ym}-01").strftime("%b %Y")


def _asset(path: str) -> str:
    """A static file's address with a fingerprint of its contents, so a browser
    fetches the new copy after a deploy instead of a cached old one."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


ASSETS = {name: _asset(name) for name in (
    "hub.css", "charts.js", "icon-32.png", "icon-64.png", "icon-180.png")}
templates.env.globals["asset"] = ASSETS.__getitem__
templates.env.globals["version"] = status.VERSION


def num(n) -> str:
    return "–" if n is None else f"{n:,.0f}"


def money(n) -> str:
    return "–" if n is None else f"${n:,.2f}"


templates.env.filters["num"] = num
templates.env.filters["money"] = money
templates.env.filters["pct"] = lambda n: "–" if n is None else f"{n:.1%}"
templates.env.filters["pos"] = lambda p: asobeast.position_label(p.get("position"), p.get("depth"))


def _config() -> settings.Settings:
    return settings.load()


def _conn(cfg: settings.Settings) -> sqlite3.Connection:
    return db.connect(cfg.database_path)


def _ratio(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def play_numbers(conn: sqlite3.Connection, app_cfg: settings.App, days: int = 7) -> dict | None:
    """Google Play's report files for one app over the last `days`, ending on the
    latest day Google has reported installs for (it runs a few days behind)."""
    if app_cfg.store != "google_play":
        return None
    end = play_reports.latest_day(conn, app_cfg, "user_installs")
    if not end:
        return {"empty": True}
    get = {m: play_reports.daily(conn, app_cfg, m) for m in
           ("user_installs", "user_uninstalls", "active_devices", "visitors", "acquisitions", "purchases")}
    installs_us = play_reports.daily(conn, app_cfg, "user_installs", "US")
    store_end = play_reports.latest_day(conn, app_cfg, "visitors")
    sales_end = play_reports.latest_day(conn, app_cfg, "purchases")
    first = metrics.shift(end, -(days - 1))
    before = (metrics.shift(first, -days), metrics.shift(first, -1))

    def period(metric: str, last: str | None) -> float | None:
        return metrics.sum_range(get[metric], metrics.shift(last, -(days - 1)), last) if last else None

    rows = []
    for label, a, b in metrics.buckets(end, days):
        store_ready = bool(store_end and a <= store_end)
        rows.append({"label": label, "first": a, "last": b,
                     "installs": metrics.sum_range(get["user_installs"], a, b),
                     "installs_us": metrics.sum_range(installs_us, a, b),
                     "uninstalls": metrics.sum_range(get["user_uninstalls"], a, b),
                     "visitors": metrics.sum_range(get["visitors"], a, b) if store_ready else None,
                     "acquisitions": metrics.sum_range(get["acquisitions"], a, b) if store_ready else None,
                     "purchases": metrics.sum_range(get["purchases"], a, b)})
    visitors, acquisitions = period("visitors", store_end), period("acquisitions", store_end)
    installs = metrics.sum_range(get["user_installs"], first, end)
    earnings = [dict(r) for r in conn.execute(
        "SELECT date, SUM(CASE WHEN metric = 'earnings_net_usd' THEN value END) AS net, "
        "SUM(CASE WHEN metric = 'earnings_gross_usd' THEN value END) AS gross FROM play_rows "
        "WHERE package = ? AND metric LIKE 'earnings_%' GROUP BY date ORDER BY date DESC LIMIT 12",
        (app_cfg.google_package,))]
    return {
        "empty": False, "end": end, "store_end": store_end, "sales_end": sales_end, "rows": rows,
        "installs_period": installs, "installs_change": installs - metrics.sum_range(get["user_installs"], *before),
        "installs_us_period": metrics.sum_range(installs_us, first, end),
        "installs_day": get["user_installs"].get(end, 0), "installs_us_day": installs_us.get(end, 0),
        "uninstalls_period": metrics.sum_range(get["user_uninstalls"], first, end),
        "active_devices": get["active_devices"].get(end),
        "visitors_period": visitors, "acquisitions_period": acquisitions,
        "conversion": _ratio(acquisitions or 0, visitors or 0),
        "purchases_period": period("purchases", sales_end) or 0,
        "earnings": earnings,
    }


def store_numbers(conn: sqlite3.Connection, app_cfg: settings.App, days: int = 7) -> dict | None:
    """Everything from App Store Connect for one app over the last `days`, or None
    for a store the hub doesn't read numbers from yet (Google Play)."""
    if app_cfg.store != "app_store":
        return None
    apple_id = app_cfg.apple_id
    downloads = metrics.downloads(conn, apple_id)
    downloads_us = metrics.downloads(conn, apple_id, "US")
    # Total counts, not unique devices: what App Store Connect and ASO Atlas show.
    impressions = db.daily_totals(conn, apple_id, "impressions")
    page_views = db.daily_totals(conn, apple_id, "page_views")
    deletions = db.daily_totals(conn, apple_id, "deletions")
    proceeds = db.daily_totals(conn, apple_id, "proceeds_usd")
    sales_day = metrics.latest_sales_day(conn)
    analytics_day = metrics.latest_analytics_day(conn, apple_id)
    analytics_since = metrics.first_analytics_day(conn, apple_id)
    # Apple's deletions report can end a day earlier than the impressions report.
    deletions_day = metrics.latest_analytics_day(conn, apple_id, metrics.INSTALLS)
    if not sales_day:
        return {"empty": True}

    def window(end: str | None) -> tuple[str, str] | None:
        return (metrics.shift(end, -(days - 1)), end) if end else None

    sales_window = window(sales_day)
    before = (metrics.shift(sales_window[0], -days), metrics.shift(sales_window[0], -1))
    analytics_window = window(analytics_day)
    deletions_window = window(deletions_day)

    funnel = None
    if analytics_window:
        shown = {k: metrics.sum_range(v, *analytics_window)
                 for k, v in (("impressions", impressions), ("page_views", page_views), ("downloads", downloads))}
        funnel = shown | {
            "end": analytics_day,
            "view_rate": _ratio(shown["page_views"], shown["impressions"]),
            "download_rate": _ratio(shown["downloads"], shown["page_views"]),
            "conversion": _ratio(shown["downloads"], shown["impressions"]),
        }

    rows = []
    for label, first, last in metrics.buckets(sales_day, days):
        ready = bool(analytics_day and analytics_since and first <= analytics_day and last >= analytics_since)
        deletions_ready = ready and deletions_day is not None and first <= deletions_day
        rows.append({"label": label, "first": first, "last": last,
                     "downloads": metrics.sum_range(downloads, first, last),
                     "downloads_us": metrics.sum_range(downloads_us, first, last),
                     "impressions": metrics.sum_range(impressions, first, last) if ready else None,
                     "page_views": metrics.sum_range(page_views, first, last) if ready else None,
                     "deletions": metrics.sum_range(deletions, first, last) if deletions_ready else None,
                     "proceeds": metrics.sum_range(proceeds, first, last) if ready else None})
    downloads_period = metrics.sum_range(downloads, *sales_window)
    return {
        "empty": False,
        "sales_day": sales_day,
        "analytics_day": analytics_day,
        # Set when the period reaches back before the analytics the hub has saved.
        "analytics_since": analytics_since if analytics_window and analytics_since > analytics_window[0] else None,
        "downloads_day": downloads.get(sales_day, 0),
        "downloads_us_day": downloads_us.get(sales_day, 0),
        "downloads_period": downloads_period,
        "downloads_change": downloads_period - metrics.sum_range(downloads, *before),
        "proceeds_day": proceeds.get(analytics_day, 0) if analytics_day else None,
        "proceeds_period": metrics.sum_range(proceeds, *analytics_window) if analytics_window else None,
        "deletions_day": deletions_day,
        "deletions_period": metrics.sum_range(deletions, *deletions_window) if deletions_window else None,
        "downloads_deletions_period": metrics.sum_range(downloads, *deletions_window) if deletions_window else None,
        "funnel": funnel,
        "rows": rows,
    }


def all_actions(conn: sqlite3.Connection, cfg: settings.Settings, app_slug: str | None = None) -> list[dict]:
    """The hub's own open actions and AsoBeast's, most urgent first."""
    sql = "SELECT * FROM hub_actions WHERE resolved_at IS NULL"
    params: list = []
    if app_slug:
        sql += " AND app_slug = ?"
        params.append(app_slug)
    items = []
    for row in conn.execute(sql, params):
        app_cfg = cfg.app(row["app_slug"])
        items.append(dict(row) | {"source": "hub", "app_label": app_cfg.label if app_cfg else row["app_slug"]})
    items += keywords.asobeast_actions(conn, cfg, app_slug)
    return sorted(items, key=lambda a: (PRIORITY_ORDER.get(a["priority"], 9), a["first_seen"]))


def play_issues(conn: sqlite3.Connection, app_cfg: settings.App) -> list[dict]:
    """Google Play crash and ANR issues, most recent first."""
    body, _ = db.latest_snapshot(conn, "play_issues", app_cfg.slug)
    issues = [i | {"label": play.issue_label(i)} for i in (body or {}).get("issues", [])]
    return sorted(issues, key=lambda i: i.get("lastErrorReportTime", ""), reverse=True)


def freshness(conn: sqlite3.Connection) -> list[dict]:
    runs = db.last_runs(conn)
    out = []
    for source, name in SOURCE_NAMES.items():
        info = runs.get(source, {"last": None, "last_ok_at": None})
        last = info["last"]
        out.append({"name": name, "ok": bool(last and last["ok"]), "never": last is None,
                    "last_ok_at": info["last_ok_at"], "message": last["message"] if last else ""})
    return out


def app_details(conn: sqlite3.Connection, apps: list[settings.App]) -> dict[str, dict]:
    """Icon and version per app, from AsoBeast's latest look at each store listing."""
    out = {}
    for a in apps:
        body, _ = db.latest_snapshot(conn, "app", a.slug)
        body = body or {}
        icon = body.get("iconUrl") or ""
        out[a.slug] = {"icon": icon if icon.startswith("https://") else None,
                       "version": (body.get("latestSnapshot") or {}).get("version")}
    return out


def _page(request: Request, name: str, cfg: settings.Settings, conn: sqlite3.Connection, **context):
    todo = sum(1 for a in all_actions(conn, cfg) if a["kind"] == "do")
    return templates.TemplateResponse(request, name, {
        "apps": cfg.apps, "sources": freshness(conn), "periods": metrics.PERIODS,
        "details": app_details(conn, cfg.apps), "todo_count": todo, "path": request.url.path, **context})


def _period(days: int) -> int:
    return days if days in metrics.PERIODS else 7


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, days: int = 7):
    cfg = _config()
    days = _period(days)
    with closing(_conn(cfg)) as conn:
        cfg = discover.with_apps(cfg, conn)
        cards = []
        for a in cfg.apps:
            open_items = all_actions(conn, cfg, a.slug)
            market = a.markets[0]
            store = store_numbers(conn, a, days)
            gp = play_numbers(conn, a, days)
            spark = (charts.sparkline(store["rows"], "downloads") if store and not store["empty"]
                     else charts.sparkline(gp["rows"], "installs") if gp and not gp["empty"] else None)
            cards.append({"app": a, "store": store, "play": gp, "ratings": keywords.ratings(conn, a), "spark": spark,
                          "crashes": play_issues(conn, a) if a.store == "google_play" else None,
                          "market": market,
                          "moves": [m for m in asobeast.moves(conn, a, cfg.keywords, market) if m["change"]][:4],
                          "todo": sum(1 for x in open_items if x["kind"] == "do"),
                          "fyi": sum(1 for x in open_items if x["kind"] == "fyi")})
        return _page(request, "overview.html", cfg, conn, cards=cards, days=days)


@app.get("/apps/{slug}", response_class=HTMLResponse)
def app_page(request: Request, slug: str, days: int = 7, show_all: bool = Query(False, alias="all")):
    cfg = _config()
    days = _period(days)
    with closing(_conn(cfg)) as conn:
        cfg = discover.with_apps(cfg, conn)
        app_cfg = cfg.app(slug)
        if app_cfg is None:
            raise HTTPException(404)
        tables = [t for t in (keywords.keyword_table(conn, app_cfg, cfg.keywords, c, show_all)
                              for c in app_cfg.markets) if t is not None]
        store = store_numbers(conn, app_cfg, days)
        gp = play_numbers(conn, app_cfg, days)
        if store and not store["empty"]:
            trend = charts.charts(store["rows"], days, num, money)
        elif gp and not gp["empty"]:
            trend = charts.charts(gp["rows"], days, num, money, charts.PLAY_CHARTS)
        else:
            trend = []
        crashes = play_issues(conn, app_cfg) if app_cfg.store == "google_play" else None
        return _page(request, "app.html", cfg, conn, app=app_cfg, store=store, play=gp, charts=trend, crashes=crashes,
                     actions=all_actions(conn, cfg, slug), days=days, tables=tables, show_all=show_all,
                     ratings=keywords.ratings(conn, app_cfg), asobeast_web=keywords.ASOBEAST_WEB)


@app.get("/actions", response_class=HTMLResponse)
def actions_page(request: Request):
    cfg = _config()
    with closing(_conn(cfg)) as conn:
        cfg = discover.with_apps(cfg, conn)
        return _page(request, "actions.html", cfg, conn, actions=all_actions(conn, cfg),
                     asobeast_web=keywords.ASOBEAST_WEB)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    cfg = _config()
    with closing(_conn(cfg)) as conn:
        cfg = discover.with_apps(cfg, conn)
        return _page(request, "settings.html", cfg, conn, s=status.page(cfg))


@app.get("/healthz")
def healthz():
    cfg = _config()
    with closing(_conn(cfg)) as conn:
        conn.execute("SELECT 1")
    return JSONResponse({"ok": True})
