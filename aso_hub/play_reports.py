"""Google Play's report files: installs, store visitors, in-app purchases and
monthly earnings, from Play Console's Cloud Storage bucket.

Play Console writes one file per app and month and rewrites the current month
every day, a few days behind (installs about 4, store visitors about 6). The
hub reads a file again only when its update time changes, and replaces that
file's rows, so a rewritten month never double counts.

Only totals are kept. The sales and earnings files also name each buyer's city
and postal code; those never reach the database.
"""
import csv
import io
import sqlite3
import urllib.parse
import zipfile

from . import db
from .play import Client, PlayError
from .settings import App, Settings

STORAGE = "https://storage.googleapis.com/storage/v1/b"
STORAGE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

# Installs overview columns -> the hub's metric names (whole app, every country).
INSTALL_COLUMNS = {"Daily User Installs": "user_installs", "Daily User Uninstalls": "user_uninstalls",
                   "Daily Device Installs": "device_installs", "Daily Device Uninstalls": "device_uninstalls",
                   "Active Device Installs": "active_devices"}
STORE_COLUMNS = {"Store listing visitors": "visitors", "Store listing acquisitions": "acquisitions"}


def _text(raw: bytes) -> str:
    """Play's statistics files are UTF-16; its sales and earnings files are UTF-8."""
    return raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")


def _rows(raw: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(_text(raw))))


def _number(value: str) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _zipped(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return [row for name in z.namelist() if name.endswith(".csv") for row in _rows(z.read(name))]


def parse(name: str, raw: bytes, package: str) -> list[tuple[str, str, str, float]]:
    """(date, territory, metric, value) rows from one report file. Territory is ""
    for the whole app, or a two-letter country (or Google's "Other")."""
    out: list[tuple[str, str, str, float]] = []
    if "/installs/" in name and name.endswith("_overview.csv"):
        for r in _rows(raw):
            out += [(r["Date"], "", metric, _number(r.get(col))) for col, metric in INSTALL_COLUMNS.items()]
    elif "/installs/" in name and name.endswith("_country.csv"):
        for r in _rows(raw):
            out.append((r["Date"], r["Country"], "user_installs", _number(r.get("Daily User Installs"))))
    elif "/store_performance/" in name and name.endswith("_country.csv"):
        for r in _rows(raw):
            # "Country / region" in the real export (checked Oct 30); other spellings Google documents too.
            country = r.get("Country / region") or r.get("Country/region") or r.get("Country") or "Other"
            out += [(r["Date"], country, metric, _number(r.get(col)))
                    for col, metric in STORE_COLUMNS.items()]
    elif "/ratings/" in name and name.endswith("_overview.csv"):
        for r in _rows(raw):
            out.append((r["Date"], "", "rating_total", _number(r.get("Total Average Rating"))))
    elif name.startswith("sales/"):
        counts: dict[tuple[str, str], float] = {}
        for r in _zipped(raw):
            if r.get("Package ID") != package:
                continue
            status = (r.get("Financial Status") or "").lower()
            metric = "purchases" if status == "charged" else "refunds" if "refund" in status else None
            if metric:
                key = (r["Order Charged Date"], metric)
                counts[key] = counts.get(key, 0) + 1
        out += [(d, "", m, v) for (d, m), v in counts.items()]
    elif name.startswith("earnings/"):
        # One file per month, in the merchant currency (USD): charges, Google's fee, taxes, refunds.
        net = gross = 0.0
        month = name.split("_")[1][:6]  # earnings_YYYYMM_...
        for r in _zipped(raw):
            if r.get("Package ID") != package:
                continue
            amount = _number(r.get("Amount (Merchant Currency)"))
            net += amount
            if r.get("Transaction Type") == "Charge":
                gross += amount
        if net or gross:
            first = f"{month[:4]}-{month[4:]}-01"
            out += [(first, "", "earnings_net_usd", round(net, 2)), (first, "", "earnings_gross_usd", round(gross, 2))]
    return out


def _wanted(name: str, package: str) -> bool:
    """The files the hub reads: this app's statistics, plus the account-wide sales and earnings."""
    if name.startswith(("sales/", "earnings/")):
        return name.endswith(".zip")
    return f"_{package}_" in name and (
        name.endswith("_overview.csv") and ("/installs/" in name or "/ratings/" in name)
        or name.endswith("_country.csv") and ("/installs/" in name or "/store_performance/" in name))


def list_files(client: Client, bucket: str) -> list[dict]:
    items, page = [], None
    while True:
        params = {"fields": "items(name,updated),nextPageToken", "maxResults": 1000} | (
            {"pageToken": page} if page else {})
        body = client.get(f"{STORAGE}/{bucket}/o", STORAGE_SCOPE, params)
        items += body.get("items", [])
        page = body.get("nextPageToken")
        if not page:
            return items


def download(client: Client, bucket: str, name: str) -> bytes:
    return client.get_bytes(f"{STORAGE}/{bucket}/o/{urllib.parse.quote(name, safe='')}", STORAGE_SCOPE,
                            {"alt": "media"})


def pull(conn: sqlite3.Connection, settings: Settings, client: Client) -> str:
    """Read every new or rewritten report file for the Google Play apps."""
    if not settings.play_bucket:
        raise PlayError("Google Play's reports bucket isn't set (PLAY_BUCKET).")
    apps = [a for a in settings.apps if a.store == "google_play"]
    # A file is read again when Google rewrites it, or when the app list changed since
    # (the sales and earnings files cover every app on the account).
    packages = ",".join(sorted(a.google_package for a in apps))
    seen = {r["name"]: (r["updated"], r["packages"])
            for r in conn.execute("SELECT name, updated, packages FROM play_files")}
    changed = [f for f in list_files(client, settings.play_bucket)
               if any(_wanted(f["name"], a.google_package) for a in apps)
               and seen.get(f["name"]) != (f["updated"], packages)]
    for f in changed:
        raw = download(client, settings.play_bucket, f["name"])
        rows = {a.google_package: parse(f["name"], raw, a.google_package) for a in apps
                if _wanted(f["name"], a.google_package)}
        with conn:  # one file at a time, all or nothing
            conn.execute("DELETE FROM play_rows WHERE file = ?", (f["name"],))
            conn.executemany("INSERT OR REPLACE INTO play_rows VALUES (?, ?, ?, ?, ?, ?)",
                             [(f["name"], package, d, t, m, v) for package, rs in rows.items() for d, t, m, v in rs])
            conn.execute("INSERT OR REPLACE INTO play_files (name, updated, fetched_at, packages) VALUES (?, ?, ?, ?)",
                         (f["name"], f["updated"], db.now(), packages))
    return f"read {len(changed)} report file{'' if len(changed) == 1 else 's'}"


def daily(conn: sqlite3.Connection, app: App, metric: str, territory: str | None = None) -> dict[str, float]:
    """Day -> value for one metric. With no territory: the whole-app figure where the
    file has one, otherwise the sum over countries (store visitors)."""
    if territory is not None:
        sql, params = ("SELECT date, SUM(value) AS v FROM play_rows WHERE package = ? AND metric = ? "
                       "AND territory = ? GROUP BY date", (app.google_package, metric, territory))
    else:
        sql, params = ("SELECT date, CASE WHEN SUM(territory = '') > 0 THEN SUM(CASE WHEN territory = '' "
                       "THEN value END) ELSE SUM(value) END AS v FROM play_rows WHERE package = ? AND metric = ? "
                       "GROUP BY date", (app.google_package, metric))
    return {r["date"]: r["v"] or 0 for r in conn.execute(sql, params)}


def latest_day(conn: sqlite3.Connection, app: App, metric: str) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM play_rows WHERE package = ? AND metric = ?",
                       (app.google_package, metric)).fetchone()
    return row["d"] if row else None
