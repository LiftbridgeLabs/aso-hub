"""What the pages show from AsoBeast: keyword tables, listing coverage, ratings,
reviews and AsoBeast's own actions. Reads the saved snapshots only."""
import re
import sqlite3

from . import asobeast, db
from .settings import App, Settings

FIELD_NAMES = {"title": "Title", "subtitle": "Subtitle", "keywordField": "Keyword field",
               "description": "Description", "shortDescription": "Short description"}
# AsoBeast's Action Center rules, in plain words.
ACTION_TITLES = {
    "keyword.add_uncovered": "Add a keyword to the listing",
    "keyword.defend": "Defend a keyword",
    "keyword.prune": "Consider dropping a keyword",
    "keyword.push_to_top10": "Push a keyword into the top 10",
    "rank.investigate_drop": "Look into a ranking drop",
    "rank.investigate_unexplained_drop": "Look into an unexplained ranking drop",
    "serp.hold_volatile": "Hold steady: these results are shifting",
    "audit.fix_factor": "Fix a listing audit item",
    "metadata.fix_lint": "Fix a listing text issue",
    "reviews.investigate_theme": "Look into a review theme",
    "reviews.investigate_rating_decline": "Ratings are slipping",
    "reviews.reply_negative": "Reply to a negative review",
    "market.improve_country": "Improve a market",
    "competitor.investigate_overtake": "A competitor overtook you",
    "listing.ship_update": "Ship a listing update",
}


def field_name(field: str) -> str:
    return FIELD_NAMES.get(field, field[:1].upper() + field[1:])


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def searched_words(audit: dict | None, market: dict | None) -> set[str]:
    """Every word the store searches for this listing: the indexed fields (on the
    App Store: title, subtitle, keyword field; not the description) plus the
    keyword field of every other language the market reads."""
    out = set()
    for f in (audit or {}).get("fields", []):
        if f.get("indexed"):
            out.update(words(f.get("value") or ""))
    for loc in (market or {}).get("localizations", []):
        if loc.get("shipped"):
            for phrase in loc.get("phrases") or []:
                out.update(words(phrase))
    return out


def keyword_table(conn: sqlite3.Connection, app: App, prefs: dict, country: str, show_all: bool) -> dict | None:
    keywords, fetched = db.latest_snapshot(conn, "keywords", f"{app.slug}:{country}")
    if keywords is None:
        return None
    compare, _ = db.latest_snapshot(conn, "compare", app.slug)
    names = {c["id"]: c["name"] for c in (compare or {}).get("competitors", [])}
    positions = {r["keywordId"]: r["positions"] for r in (compare or {}).get("rows", [])}
    audit, _ = db.latest_snapshot(conn, "audit", f"{app.slug}:{country}")
    coverage = {c["keywordId"]: c for c in (audit or {}).get("coverage", [])}
    market, _ = db.latest_snapshot(conn, "market", f"{app.slug}:{country}")
    searched = searched_words(audit, market)
    week = {m["keywordId"]: m for m in asobeast.moves(conn, app, {}, country)}

    rows, hidden = [], 0
    for k in keywords:
        is_hidden = asobeast.is_hidden(k, prefs)
        if is_hidden and not show_all:
            hidden += 1
            continue
        mine = asobeast.ranked({"position": k["latestPosition"], "depth": k["latestDepth"]})
        ahead = sorted((p, names.get(cid, "?")) for cid, p in positions.get(k["keywordId"], {}).items()
                       if p is not None and p < mine)
        cov = coverage.get(k["keywordId"])
        rows.append({
            "text": k["text"],
            "manual": k.get("source") == "MANUAL",
            "hidden": is_hidden,
            "position": asobeast.position_label(k["latestPosition"], k["latestDepth"]),
            "sort": (mine, -(k.get("traffic") or 0)),
            "week": week[k["keywordId"]]["change"] if k["keywordId"] in week else None,
            "popularity": k.get("traffic"),
            "difficulty": k.get("difficulty"),
            "ahead": len(ahead),
            "best_competitor": f"{ahead[0][1]} #{ahead[0][0]}" if ahead else None,
            "covered": [field_name(f["field"]) + (f" ({f['localization']})" if f.get("localization") else "")
                        for f in (cov or {}).get("fields", []) if f.get("covered")],
            # Apple matches words across fields, so a phrase no single field holds can still be covered.
            "missing_words": [w for w in words(k["text"]) if w not in searched] if searched else [],
            "uncovered": bool(cov and cov.get("uncovered")),
        })
    rows.sort(key=lambda r: r["sort"])
    fields = [{"name": field_name(f["field"]), "value": f.get("value") or "", "chars": f.get("chars"),
               "limit": f.get("limit"), "issues": f.get("issues") or []} for f in (audit or {}).get("fields", [])]
    other_languages = [loc["localization"] for loc in (market or {}).get("localizations", [])
                       if loc.get("shipped") and loc.get("role") != "default"]
    return {"country": country, "rows": rows, "hidden": hidden, "fields": fields, "fetched": fetched,
            "word_check": bool(searched), "other_languages": other_languages}


def ratings(conn: sqlite3.Connection, app: App, count: int = 5) -> dict | None:
    detail, _ = db.latest_snapshot(conn, "app", app.slug)
    if detail is None:
        return None
    snap = detail.get("latestSnapshot") or {}
    reviews, _ = db.latest_snapshot(conn, "reviews", app.slug)
    items = sorted((reviews or {}).get("reviews", []), key=lambda r: r["reviewedAt"], reverse=True)
    return {"average": snap.get("ratingAvg"), "count": snap.get("ratingCount"), "reviews": items[:count]}


def _action_detail(item: dict) -> str:
    scope, ev = item.get("scope") or {}, item.get("evidence") or {}
    parts = []
    if scope.get("keywordText"):
        parts.append(f"“{scope['keywordText']}”")
    if scope.get("country"):
        parts.append(scope["country"].upper())
    if ev.get("yourPosition") is not None:
        was = f" (was #{ev['previousPosition']})" if ev.get("previousPosition") is not None else ""
        parts.append(f"you're #{ev['yourPosition']}{was}")
    if ev.get("marketVisibility") is not None and ev.get("homeVisibility") is not None:
        parts.append(f"visibility {ev['marketVisibility']:.1f} vs {ev['homeVisibility']:.1f} in "
                     f"{str(ev.get('homeCountry', 'home')).upper()}")
    entrants = [e for e in ev.get("entrants") or [] if e.get("position") is not None]
    if entrants:
        parts.append("new nearby: " + ", ".join(f"{e['title']} #{e['position']}" for e in entrants[:2]))
    parts.append(f"impact {item.get('impact', 0)}/100")
    return " · ".join(parts)


def asobeast_actions(conn: sqlite3.Connection, settings: Settings, app_slug: str | None = None) -> list[dict]:
    """AsoBeast's open and snoozed actions, shaped like the hub's own."""
    body, _ = db.latest_snapshot(conn, "actions", "all")
    ids, _ = db.latest_snapshot(conn, "ids", "all")
    slugs = {aid: slug for slug, aid in (ids or {}).items()}
    out = []
    for item in (body or {}).get("items", []):
        slug = slugs.get((item.get("scope") or {}).get("appId"))
        if item.get("status") not in ("OPEN", "SNOOZED") or (app_slug and slug != app_slug):
            continue
        app = settings.app(slug) if slug else None
        out.append({"source": "asobeast", "kind": "do", "priority": item.get("priority", "low"),
                    "title": ACTION_TITLES.get(item["rule"], item["rule"]), "detail": _action_detail(item),
                    "first_seen": item.get("firstSeenAt", ""), "app_slug": slug,
                    "app_label": app.label if app else (item.get("scope") or {}).get("appName", ""),
                    "snoozed": item.get("status") == "SNOOZED", "link": settings.asobeast_web_url,
                    "id": item.get("id", ""), "reopen_count": item.get("reopenCount", 0)})
    return out
