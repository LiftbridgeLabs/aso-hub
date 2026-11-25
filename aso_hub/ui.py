"""What every page shares: the templates, the settings in use, and the frame around a page."""
import datetime as dt
import hashlib
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import asobeast, db, keywords, metrics, overlay, settings, status

SOURCE_NAMES = {"asc": "App Store Connect", "asobeast": "AsoBeast", "play": "Google Play"}
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
HERE = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["short_date"] = metrics.short_date
templates.env.filters["month"] = lambda ym: dt.date.fromisoformat(f"{ym}-01").strftime("%b %Y")


def _asset(path: str) -> str:
    """A static file's address with a fingerprint of its contents, so a browser
    fetches the new copy after a deploy instead of a cached old one."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


ASSETS = {name: _asset(name) for name in (
    "hub.css", "charts.js", "icon-32.png", "icon-64.png", "icon-180.png",
    "forms.js")}
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



def conn_and_config() -> tuple[settings.Settings, sqlite3.Connection]:
    """The settings in use now, and an open database for the request."""
    base = settings.load()
    conn = db.connect(base.database_path)
    return overlay.effective(base, conn), conn


def hub_url(request: Request) -> str:
    """The hub's own address: ASO_HUB_URL, or the one this request came to."""
    own = os.environ.get("ASO_HUB_URL", "").rstrip("/")
    return own or str(request.base_url).rstrip("/")


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


def page(request: Request, name: str, cfg: settings.Settings, conn: sqlite3.Connection, code: int = 200,
         **context):
    """A signed-in page, with the app links and each source's freshness around it."""
    todo = sum(1 for a in all_actions(conn, cfg) if a["kind"] == "do")
    return templates.TemplateResponse(request, name, {
        "apps": cfg.apps, "sources": freshness(conn), "periods": metrics.PERIODS,
        "details": app_details(conn, cfg.apps), "todo_count": todo, "path": request.url.path,
        "saved": request.query_params.get("saved", ""), **context}, status_code=code)


def saved(path: str, message: str) -> RedirectResponse:
    """After a change: back to the page, saying what happened."""
    return RedirectResponse(f"{path}?{urlencode({'saved': message})}", status_code=303)
