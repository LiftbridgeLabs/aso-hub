"""Fill a throwaway database with made-up numbers, to look at the pages or take screenshots.

    python demo/seed.py /tmp/demo.db
    HUB_CONFIG=demo/config.toml DATABASE_PATH=/tmp/demo.db python -m aso_hub serve

Everything here is invented (the apps in demo/config.toml are examples too). Run from the repo root.
"""
import datetime as dt
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aso_hub import asc, db, metrics, rules, settings  # noqa: E402

rng = random.Random(7)  # noqa: S311 (made-up numbers, not security)
TODAY = dt.date.today()
DAYS = 60
# Per app: (daily first-time downloads, impressions per download, page views per download, paid share)
SHAPE = {"lumen-ios": (14, 55, 9, .22), "kettle-ios": (6, 40, 8, 0), "tandem-ios": (3, 30, 7, .1),
         "atlas-ios": (9, 45, 8, 0)}
KEYWORDS = {
    "lumen-ios": [("offline audiobook player", 14, 41), ("audiobook speed control", 22, 18), ("sleep timer", 9, 28),
                  ("podcast and audiobook", 31, 33), ("listen offline", 6, 7), ("self hosted media", 48, 51),
                  ("bookmark audio", 12, 12), ("chapter list", 61, 64)],
    "kettle-ios": [("read it later", 19, 18), ("rss reader", 33, 29), ("save articles", 17, 21),
                   ("feed organizer", 8, 5)],
    "tandem-ios": [("recipe scaler", 4, 4), ("cooking timer", 27, 25), ("meal planner", 55, 52)],
    "atlas-ios": [("photo map", 11, 15), ("trip photos", 24, 22), ("geotag photos", 7, 9)],
}


def day_str(back: int) -> str:
    return (TODAY - dt.timedelta(days=back)).isoformat()


def wobble(base: float, d: int) -> float:
    week = 1 + .18 * math.sin(d / 7 * 2 * math.pi)
    growth = 1 + .006 * (DAYS - d)
    return max(0, base * week * growth * rng.uniform(.7, 1.3))


def seed_numbers(conn, cfg) -> None:
    for app in cfg.apps:
        if app.store != "app_store" or app.slug not in SHAPE:
            continue
        daily, imp_per, pv_per, paid = SHAPE[app.slug]
        disc, inst, buys = {}, {}, {}
        for d in range(DAYS, 0, -1):
            day = day_str(d)
            downloads = round(wobble(daily, d))
            us = round(downloads * .6)
            with conn:
                conn.executemany("INSERT INTO sales_rows VALUES (?, ?, ?, ?, ?, ?)",
                                 [(day, app.apple_id, "US", "iPhone", "first_time_units", us),
                                  (day, app.apple_id, "GB", "iPhone", "first_time_units", downloads - us)])
                conn.execute("INSERT OR REPLACE INTO sales_days VALUES (?, ?, 1)", (day, db.now()))
            impressions = round(downloads * wobble(imp_per, d))
            views = round(downloads * wobble(pv_per, d))
            disc[(day, "US", "iPhone", "impressions")] = (impressions, round(impressions * .8))
            disc[(day, "US", "iPhone", "page_views")] = (views, round(views * .85))
            inst[(day, "US", "iPhone", "installs")] = (downloads, downloads)
            inst[(day, "US", "iPhone", "deletions")] = (round(downloads * .25), round(downloads * .25))
            if paid:
                buys[(day, "US", "iPhone", "proceeds_usd")] = (round(downloads * paid * 3.4, 2), None)
                buys[(day, "US", "iPhone", "purchases")] = (round(downloads * paid), round(downloads * paid))
        for report, rows in ((metrics.DISCOVERY, disc), (metrics.INSTALLS, inst),
                             ("App Store Purchases Standard", buys)):
            if rows:
                asc.save_instance(conn, app.apple_id, report, day_str(0), f"demo-{app.slug}-{report}", rows)
    for source in ("asc", "asobeast", "play"):
        db.finish_run(conn, db.start_run(conn, source), True, "demo data")


def seed_keywords(conn, cfg) -> None:
    for app in cfg.apps:
        for country in app.markets[:1]:
            rows, series = [], []
            for i, (text, now, before) in enumerate(KEYWORDS.get(app.slug, [])):
                kid = f"{app.slug}-{i}"
                rows.append({"keywordId": kid, "text": text, "country": country, "source": "MANUAL",
                             "latestPosition": now, "latestDepth": 200, "previousPosition": before,
                             "traffic": round(rng.uniform(1.5, 6), 1), "difficulty": round(rng.uniform(1.5, 5.5), 1),
                             "tags": []})
                path = [round(before + (now - before) * min(1, j / 8) + rng.uniform(-2, 2)) for j in range(15)]
                if text == "sleep timer":  # steady near #9, then three bad checks: raises a to-do
                    path = [9, 8, 10, 9, 9, 10, 8, 9, 9, 10, 9, 28, 29, 30, 28]
                series.append({"keywordId": kid, "text": text, "store": "APP_STORE", "country": country, "points": [
                    {"date": day_str(14 - j), "position": max(1, p), "depth": 200} for j, p in enumerate(path)]})
            if rows:
                db.save_snapshot(conn, day_str(0), "keywords", f"{app.slug}:{country}", rows)
                db.save_snapshot(conn, day_str(0), "rankings", app.slug, {"series": series})
        if app.store == "app_store":
            db.save_snapshot(conn, day_str(0), "app", app.slug,
                             {"iconUrl": "", "latestSnapshot": {"version": "1.4", "ratingAvg": 4.6,
                                                               "ratingCount": 38 if app.slug == "lumen-ios" else 9}})
            reviews = [{"reviewId": f"{app.slug}-r1", "score": 5, "title": "Does what it says",
                        "text": "Simple, fast, and nothing extra.", "reviewedAt": day_str(9) + "T12:00:00Z",
                        "version": "1.3", "repliedAt": day_str(8)}]
            if app.slug == "lumen-ios":  # one review waiting for a reply raises a to-do
                reviews.append({"reviewId": "lumen-r2", "score": 4, "title": "Great, one wish",
                                "text": "Would love a widget.", "reviewedAt": day_str(1) + "T09:30:00Z",
                                "version": "1.4"})
            db.save_snapshot(conn, day_str(0), "reviews", app.slug, {"reviews": reviews})


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
    if path.exists():
        sys.exit(f"{path} already exists; pick a new name so nothing real is overwritten")
    import os
    os.environ["DATABASE_PATH"] = str(path)
    os.environ.setdefault("HUB_CONFIG", str(Path(__file__).resolve().parent / "config.toml"))
    cfg = settings.load()
    conn = db.connect(path)
    seed_numbers(conn, cfg)
    seed_keywords(conn, cfg)
    rules.evaluate(cfg, conn)
    conn.close()
    print(f"Wrote {path}. Run it with: HUB_CONFIG=demo/config.toml DATABASE_PATH={path} python -m aso_hub serve")


if __name__ == "__main__":
    main()
