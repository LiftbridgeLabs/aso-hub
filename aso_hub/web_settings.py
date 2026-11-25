"""Settings → Connections, Apps, Alerts and Schedule.

Keys, tokens and webhooks can be saved, replaced and removed here, never read
back. A saved secret is only ever sent where it was saved for.
"""
import datetime as dt
import json
import re
import sqlite3
import zoneinfo
from contextlib import closing

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import asc, asobeast, db, discover, forms, notify, overlay, play, scheduler, status
from .settings import App, Settings
from .ui import conn_and_config, page, saved

router = APIRouter()
SLUG = re.compile(r"[^a-z0-9]+")


class Refused(Exception):
    """Why a change wasn't made, in plain words."""


# Connections

def _secret_text(name: str, value: str) -> str:
    """The pasted value, checked for being the right kind of thing."""
    value = value.strip()
    if name == "asc-key.p8" and "PRIVATE KEY" not in value:
        raise Refused("That isn't an App Store Connect key: the .p8 file starts with “BEGIN PRIVATE KEY”.")
    if name == "google-play.json":
        try:
            body = json.loads(value)
            complete = body["client_email"] and body["private_key"]
        except (ValueError, KeyError, TypeError):
            complete = False
        if not complete:
            raise Refused("That isn't a Google service account key: choose the .json file Google Cloud gave you.")
    if (name == "discord-webhook" or name.startswith("alerts-")) and not value.startswith(notify.WEBHOOK_PREFIXES):
        raise Refused("That isn't a Discord webhook address (it starts https://discord.com/api/webhooks/).")
    return value


def _save_ids(conn: sqlite3.Connection, form: forms.Form, names: tuple[str, ...]) -> None:
    ids = db.get_setting(conn, "ids", {})
    for name in names:
        value = form.text(name)
        if value:
            ids[name] = value
        elif form.on(f"clear_{name}"):
            ids.pop(name, None)
    db.set_setting(conn, "ids", ids)


def _save_connection(cfg: Settings, conn: sqlite3.Connection, which: str, form: forms.Form) -> str:
    if which == "asc":
        key = form.text("key")
        key = _secret_text("asc-key.p8", key) if key else None
        _save_ids(conn, form, ("asc_issuer_id", "asc_key_id", "asc_vendor_number"))
        if key:
            overlay.save_secret(conn, cfg, "asc-key.p8", key)
    elif which == "play":
        key = form.text("key")
        key = _secret_text("google-play.json", key) if key else None
        bucket = form.text("play_bucket").removeprefix("gs://").strip("/")
        if bucket and not re.fullmatch(r"[a-z0-9._-]{3,222}", bucket):
            raise Refused("A bucket name looks like pubsite_prod_1234567890.")
        form["play_bucket"] = [bucket]
        _save_ids(conn, form, ("play_bucket",))
        if key:
            overlay.save_secret(conn, cfg, "google-play.json", key)
    elif which == "asobeast":
        token = form.text("token")
        for name in ("asobeast_url", "asobeast_web_url"):
            value = form.text(name).rstrip("/")
            if value and not re.match(r"https?://[^\s/]+", value):
                raise Refused("An address starts with http:// or https://.")
            form[name] = [value]
        # Otherwise whoever could change the address would receive the saved token.
        moving = form.text("asobeast_url") and form.text("asobeast_url") != cfg.asobeast_url
        if (moving or form.on("clear_asobeast_url")) and not token:
            raise Refused("Enter the token again when you change AsoBeast's address.")
        _save_ids(conn, form, ("asobeast_url", "asobeast_web_url"))
        if token:
            overlay.save_secret(conn, cfg, "asobeast-token", token)
    else:
        raise Refused("Unknown connection.")
    return "Saved. Use Test to check it."


def _remove_secret(cfg: Settings, conn: sqlite3.Connection, name: str) -> str:
    if name not in overlay.SECRET_FIELDS.values() and not re.fullmatch(r"alerts-[a-z0-9-]+", name):
        raise Refused("Unknown secret.")
    overlay.save_secret(conn, cfg, name, None)
    if name == "asobeast-token":  # a token from a file mustn't go to an address saved here
        ids = db.get_setting(conn, "ids", {})
        ids.pop("asobeast_url", None)
        db.set_setting(conn, "ids", ids)
    return "Removed from the hub."


def test_connection(cfg: Settings, which: str, transport: httpx.BaseTransport | None = None) -> str:
    """Try a saved connection for real; raises Refused with the reason."""
    try:
        if which == "asc":
            key = cfg.read_secret(cfg.asc_key_path)
            if not (cfg.asc_issuer_id and cfg.asc_key_id and cfg.asc_vendor_number and key):
                raise Refused("Fill in the issuer id, key id, vendor number and key first.")
            client = asc.Client(cfg.asc_issuer_id, cfg.asc_key_id, key, transport=transport)
            client.sales_report(cfg.asc_vendor_number, (dt.date.today() - dt.timedelta(days=2)).isoformat())
            return "Apple accepted the key and vendor number."
        if which == "play":
            client = play.client_for(cfg)
            app = next((a for a in cfg.apps if a.store == "google_play"), None)
            if app:
                play.error_issues(client, app.google_package)
                return f"Google accepted the key and read {app.label}."
            client.check()
            return "Google accepted the key. Add an Android app to check it can read one."
        if which == "asobeast":
            token = cfg.read_secret(cfg.asobeast_token_file)
            if not token:
                raise Refused("Save the AsoBeast token first.")
            found = asobeast.Client(cfg.asobeast_url, token, transport=transport).get("/apps")
            return f"AsoBeast answered: {len(found)} app{'' if len(found) == 1 else 's'}."
        if which == "discord-webhook" or which.startswith("alerts-"):
            path = (cfg.discord_webhook_file if which == "discord-webhook"
                    else cfg.alerts_webhook_file(which.removeprefix("alerts-")))
            url = cfg.read_secret(path)
            if not url or not url.startswith(notify.WEBHOOK_PREFIXES):
                raise Refused("Save a Discord webhook first.")
            with httpx.Client(transport=transport, timeout=20) as client:
                notify._post(client, url, "ASO Hub: a test message from Settings.")
            return "Test message sent. Check the Discord channel."
    except Refused:
        raise
    except (asc.AscError, asobeast.AsoBeastError, play.PlayError, notify.NotifyError) as e:
        raise Refused(str(e)) from None
    except httpx.HTTPError as e:
        raise Refused(f"Couldn't connect ({type(e).__name__}). Check the address.") from None
    except Exception as e:  # a key that isn't a key, an answer that isn't an answer
        raise Refused(f"That didn't work ({type(e).__name__}). Check what you saved.") from None
    raise Refused("Unknown connection.")


def _connections_page(request: Request, cfg, conn, **context):
    return page(request, "settings/connections.html", cfg, conn, state=status.secret_states(cfg),
                ids=db.get_setting(conn, "ids", {}), hub=cfg, **context)


# Apps

def _markets(text: str) -> list[str]:
    markets = list(dict.fromkeys(text.lower().replace(",", " ").split()))
    if not markets or not all(discover.COUNTRY.fullmatch(m) for m in markets):
        raise Refused("Markets are two-letter country codes, such as: us, gb, de")
    return markets


def _app_name(text: str) -> str:
    if not text:
        raise Refused("Give the app a name.")
    return text[:60]


def _keep_app(conn: sqlite3.Connection, app: App) -> None:
    mine = [a for a in db.get_setting(conn, "apps", []) if a["slug"] != app.slug]
    id_key = "apple_id" if app.store == "app_store" else "google_package"
    mine.append({"slug": app.slug, "name": app.name, "store": app.store, "markets": app.markets, id_key: app.store_id})
    db.set_setting(conn, "apps", mine)


def _change_apps(cfg: Settings, conn: sqlite3.Connection, action: str, form: forms.Form) -> str:
    hidden = db.get_setting(conn, "hidden", [])
    if action == "add":
        store, sid = form.text("store"), form.text("store_id")
        if store not in discover.STORE_IDS or not discover.STORE_IDS[store].fullmatch(sid):
            raise Refused("Enter the App Store number (digits) or the Google Play package, such as com.example.app.")
        if any(a.store == store and a.store_id == sid for a in cfg.apps):
            raise Refused("That app is already on the list.")
        name = _app_name(form.text("name"))
        base = SLUG.sub("-", name.lower()).strip("-")[:40] or "app"
        slug, n = f"{base}-{discover.SUFFIX[store]}", 2
        while cfg.app(slug):
            slug, n = f"{base}-{n}-{discover.SUFFIX[store]}", n + 1
        id_key = "apple_id" if store == "app_store" else "google_package"
        db.set_setting(conn, "hidden", [h for h in hidden if h != sid] or None)
        _keep_app(conn, App(slug=slug, name=name, store=store, markets=_markets(form.text("markets", "us")),
                            **{id_key: sid}))
        return f"{name} added. Its numbers arrive with the next pull."
    if action == "show":
        db.set_setting(conn, "hidden", [h for h in hidden if h != form.text("store_id")] or None)
        return "Shown again."
    if action == "auto":
        db.set_setting(conn, "add_apps", form.on("add_apps"))
        return "Saved."
    app = cfg.app(form.text("slug"))
    if app is None:
        raise Refused("That app isn't on the list.")
    if action == "edit":
        _keep_app(conn, App(slug=app.slug, name=_app_name(form.text("name")), store=app.store,
                            markets=_markets(form.text("markets")), apple_id=app.apple_id,
                            google_package=app.google_package))
        return "Saved."
    if action == "reset":
        db.set_setting(conn, "apps", [a for a in db.get_setting(conn, "apps", []) if a["slug"] != app.slug] or None)
        return "Back to what config.toml or AsoBeast says (an app added here is removed)."
    if action == "hide":
        db.set_setting(conn, "hidden", [*hidden, app.store_id])
        return f"{app.label} hidden. Its saved numbers are kept."
    raise Refused("Unknown change.")


def _apps_page(request: Request, cfg, conn, **context):
    return page(request, "settings/apps.html", cfg, conn, hidden=db.get_setting(conn, "hidden", []),
                mine={a["slug"] for a in db.get_setting(conn, "apps", [])}, add_apps=cfg.add_apps, **context)


# Alerts

def _number(text: str, like, name: str):
    try:
        value = int(text) if isinstance(like, int) else float(text)
    except ValueError:
        raise Refused(f"“{name}” needs a {'whole ' if isinstance(like, int) else ''}number.") from None
    if not 0 <= value <= 100_000:
        raise Refused(f"“{name}” is out of range.")
    if name.startswith("days") and not 1 <= value <= 365:
        raise Refused(f"“{name}” is between 1 and 365.")
    return value


def _change_alerts(cfg: Settings, conn: sqlite3.Connection, action: str, form: forms.Form) -> str:
    if action == "rules":
        changed = {}
        for key, rule in cfg.rules.items():
            values: dict = {"enabled": form.on(f"{key}.enabled")}
            for name, now in rule.items():
                if name == "enabled":
                    continue
                label = status.THRESHOLDS.get(name, name)
                if isinstance(now, dict):  # a lowest number per app; blank means the app isn't checked
                    values[name] = {a.slug: _number(form.text(f"{key}.{name}.{a.slug}"), 0.0, label)
                                    for a in cfg.apps if form.text(f"{key}.{name}.{a.slug}")}
                elif f"{key}.{name}" in form:
                    values[name] = _number(form.text(f"{key}.{name}"), now, label)
            changed[key] = values
        db.set_setting(conn, "rules", changed)
        return "Rules saved. They apply at the next pull."
    if action == "keywords":
        words = list(dict.fromkeys(form.text("keep").lower().replace(",", " ").split()))
        db.set_setting(conn, "keywords", {"hide_single_words": form.on("hide"), "keep_single_words": words})
        return "Saved."
    if action == "digest":
        top = _number(form.text("top_issues", "3"), 0, "Android issues listed")
        db.set_setting(conn, "crash_digest", {"apps": form.all("apps"), "top_issues": min(top, 10)})
        return "Weekly crash digest saved."
    if action == "webhook":
        name = form.text("name")
        if name != "discord-webhook" and not re.fullmatch(r"alerts-[a-z0-9-]+", name):
            raise Refused("Unknown webhook.")
        if not form.text("url"):
            raise Refused("Paste the webhook address first.")
        overlay.save_secret(conn, cfg, name, _secret_text(name, form.text("url")))
        return "Webhook saved. Use Test to check it."
    raise Refused("Unknown change.")


def _alerts_page(request: Request, cfg, conn, **context):
    names = list(dict.fromkeys(a.name for a in cfg.apps))
    return page(request, "settings/alerts.html", cfg, conn, hub=cfg, rules=cfg.rules, names=names,
                rule_names=status.RULES, thresholds=status.THRESHOLDS, webhooks=status.webhooks(cfg), **context)


# Schedule

def _change_schedule(cfg: Settings, conn: sqlite3.Connection, form: forms.Form) -> str:
    zone = form.text("timezone")
    if zone not in zoneinfo.available_timezones():
        raise Refused("Choose a time zone from the list.")
    times = sorted(set(form.text("pull_times").replace(",", " ").split()))
    times = [t.zfill(5) for t in times]
    if not 1 <= len(times) <= 12 or not all(scheduler.TIME.fullmatch(t) for t in times):
        raise Refused("Pull times are 24-hour times such as 07:00, 13:00 (at least one).")
    day, at = form.text("digest_day"), form.text("digest_time")
    if day not in "0123456" or len(day) != 1 or not scheduler.TIME.fullmatch(at):
        raise Refused("Choose a day and a time for the crash digest.")
    plan = {"enabled": form.on("enabled"), "timezone": zone, "pull_times": sorted(times), "digest_day": int(day),
            "digest_time": at}
    db.set_setting(conn, "schedule", plan)
    # New times count from now: a time already past today doesn't run on saving.
    done = db.get_setting(conn, "schedule_done", {})
    for job in list(done):
        latest, _ = scheduler.slots(plan, job, dt.datetime.now(dt.UTC))
        if latest:
            done[job] = latest.astimezone(dt.UTC).isoformat()
    db.set_setting(conn, "schedule_done", done)
    return "Schedule saved."


def _schedule_page(request: Request, cfg, conn, **context):
    plan = scheduler.schedule(conn)
    tz = scheduler.zone(plan["timezone"])
    now = dt.datetime.now(dt.UTC)
    upcoming = {job: scheduler.slots(plan, job, now)[1] for job in ("pull", "digest")}
    fmt = "%a, %b %-d at %-I:%M %p"
    return page(request, "settings/schedule.html", cfg, conn, plan=plan, days=scheduler.DAYS,
                zones=sorted(zoneinfo.available_timezones()), running=scheduler.RUNNING.locked(),
                upcoming={k: v.astimezone(tz).strftime(fmt) for k, v in upcoming.items() if v},
                runs=db.last_runs(conn), **context)


PAGES = {"connections": _connections_page, "apps": _apps_page, "alerts": _alerts_page, "schedule": _schedule_page}


@router.get("/settings/{section}", response_class=HTMLResponse)
def show(request: Request, section: str):
    if section not in PAGES:
        return HTMLResponse("Not found", status_code=404)
    cfg, conn = conn_and_config()
    with closing(conn):
        return PAGES[section](request, cfg, conn)


def _apply(request: Request, cfg, conn, section: str, action: str, form: forms.Form):
    back = f"/settings/{section}"
    if action == "test":
        return saved(back, test_connection(cfg, form.text("what")))
    if action == "remove":
        return saved(back, _remove_secret(cfg, conn, form.text("name")))
    if section == "connections" and action == "save":
        return saved(back, _save_connection(cfg, conn, form.text("which"), form))
    if section == "apps":
        return saved(back, _change_apps(cfg, conn, action, form))
    if section == "alerts":
        return saved(back, _change_alerts(cfg, conn, action, form))
    if section == "schedule" and action == "save":
        return saved(back, _change_schedule(cfg, conn, form))
    if section == "schedule" and action in ("pull", "digest"):
        if not scheduler.run_now(action):
            raise Refused("A pull or digest is already running. Try again in a minute.")
        return saved(back, "Started. Reload in a minute to see the result.")
    raise Refused("Unknown change.")


@router.post("/settings/{section}/{action}", response_class=HTMLResponse)
async def change(request: Request, section: str, action: str):
    if section not in PAGES:
        return HTMLResponse("Not found", status_code=404)
    form = await forms.read(request)

    def run():
        cfg, conn = conn_and_config()
        with closing(conn):
            try:
                return _apply(request, cfg, conn, section, action, form)
            except Refused as e:
                return PAGES[section](request, cfg, conn, code=400, error=str(e))

    return await run_in_threadpool(run)
