"""Post new to-dos to Discord, through the aso-hub webhook in #marketing-visibility.

Only "To do" items post; "Worth knowing" stays on the pages, and snoozed
AsoBeast items stay quiet. Each item posts once: when it opens, and again only
if it resolves and comes back. The webhook URL is a credential, so it is never
printed, not even in an error.
"""
import os
import sqlite3

import httpx

from . import db, keywords
from .settings import Settings

HUB_URL = os.environ.get("ASO_HUB_URL", "http://localhost:8690").rstrip("/")
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
LIMIT = 1900  # Discord allows 2000 characters
DETAIL_CHARS = 220
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

class NotifyError(Exception):
    pass


def open_todos(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Every open to-do, hub and AsoBeast, with the key it is remembered by once posted."""
    out = []
    rows = conn.execute("SELECT rule, app_slug, key, title, detail, priority, first_seen, link FROM hub_actions "
                        "WHERE resolved_at IS NULL AND kind = 'do'").fetchall()
    for r in rows:
        app = settings.app(r["app_slug"])
        out.append({"alert_key": f"hub:{r['rule']}:{r['app_slug']}:{r['key']}:{r['first_seen']}",
                    "priority": r["priority"], "title": r["title"], "detail": r["detail"],
                    "app_label": app.label if app else r["app_slug"], "link": r["link"]})
    for item in keywords.asobeast_actions(conn, settings):
        if not item["snoozed"]:
            # The link to AsoBeast is the same for every item, so the Actions page link covers it.
            out.append(item | {"alert_key": f"asobeast:{item['id']}:{item['reopen_count']}", "link": None})
    out.sort(key=lambda i: PRIORITY_ORDER.get(i["priority"], 3))
    return out


def _line(item: dict) -> str:
    detail = item["detail"]
    if len(detail) > DETAIL_CHARS:
        detail = detail[:DETAIL_CHARS - 1].rstrip() + "…"
    line = f"• **{item['title']}** · {item['app_label']} · {item['priority']}\n  {detail}"
    return line + (f"\n  <{item['link']}>" if item.get("link") else "")


def message(items: list[dict]) -> str:
    noun = "to-do" if len(items) == 1 else "to-dos"
    return "\n".join([f"**ASO Hub: {len(items)} new {noun}**", *map(_line, items), f"<{HUB_URL}/actions>"])


def batches(items: list[dict]) -> list[list[dict]]:
    """Split the to-dos into messages that each fit Discord's limit, keeping their order."""
    out: list[list[dict]] = []
    for item in items:
        if out and len(message(out[-1] + [item])) <= LIMIT:
            out[-1].append(item)
        else:
            out.append([item])
    return out


def webhook(settings: Settings) -> str | None:
    url = settings.read_secret(settings.discord_webhook_file)
    if url and not url.startswith(WEBHOOK_PREFIXES):
        raise NotifyError("The saved Discord webhook isn't a Discord webhook address.")
    return url


def send(conn: sqlite3.Connection, settings: Settings, transport: httpx.BaseTransport | None = None) -> str:
    url = webhook(settings)
    if not url:
        return "no Discord webhook saved; nothing posted"
    sent = {r["key"] for r in conn.execute("SELECT key FROM sent_alerts")}
    new = [i for i in open_todos(conn, settings) if i["alert_key"] not in sent]
    if not new:
        return "no new to-dos"
    posted = 0
    with httpx.Client(transport=transport, timeout=20, headers={"User-Agent": "aso-hub"}) as client:
        for batch in batches(new):
            _post(client, url, message(batch))
            # Marked only once Discord confirms it saved this message, so a lost one is retried.
            with conn:
                conn.executemany("INSERT OR IGNORE INTO sent_alerts (key, sent_at) VALUES (?, ?)",
                                 [(i["alert_key"], db.now()) for i in batch])
            posted += len(batch)
    return f"posted {posted} new to-do{'' if posted == 1 else 's'}"


def _post(client: httpx.Client, url: str, text: str) -> None:
    payload = {"content": text, "allowed_mentions": {"parse": []}, "flags": 4}  # 4: no link previews
    try:
        # wait=true: Discord answers only after the message is saved.
        response = client.post(url, params={"wait": "true"}, json=payload)
    except httpx.HTTPError as e:
        raise NotifyError(f"Couldn't reach Discord ({type(e).__name__}).") from None
    if response.status_code != 200:
        raise NotifyError(f"Discord answered HTTP {response.status_code}.")
