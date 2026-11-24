"""App Store Connect: downloads, impressions, page views, deletions, proceeds.

Reads the Analytics Reports API with the hub's own team key (Sales role).
ASO Atlas already created an ongoing report request for each app, and a
second create fails, so this only lists and reads existing requests.
"""
import csv
import datetime as dt
import gzip
import io
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterator

import httpx
import jwt

from . import db

API = "https://api.appstoreconnect.apple.com"
# Apple revises recent days for a while; re-read files this new on every pull.
REFRESH_DAYS = 5


class AscError(Exception):
    """A problem worth showing on the page, in plain words."""


class NoReportRequest(AscError):
    """The app has no ongoing analytics report request yet (a new app). Its sales
    still count; only the store page numbers wait until a request exists."""


# Each report the hub reads, and how one of its rows turns into
# (metric, value, unique devices) entries. Rows that map to nothing are skipped.
def _downloads(row: dict) -> list[tuple[str, float, float | None]]:
    kind = {"First-time download": "first_time_downloads", "Redownload": "redownloads"}.get(row["Download Type"])
    return [(kind, _num(row["Counts"]), None)] if kind else []


def _discovery(row: dict) -> list[tuple[str, float, float | None]]:
    kind = {"Impression": "impressions", "Page view": "page_views"}.get(row["Event"])
    return [(kind, _num(row["Counts"]), _num(row["Unique Counts"]))] if kind else []


def _installs(row: dict) -> list[tuple[str, float, float | None]]:
    kind = {"Install": "installs", "Delete": "deletions"}.get(row["Event"])
    return [(kind, _num(row["Counts"]), _num(row["Unique Devices"]))] if kind else []


def _purchases(row: dict) -> list[tuple[str, float, float | None]]:
    return [("purchases", _num(row["Purchases"]), _num(row["Paying Users"])),
            ("proceeds_usd", _num(row["Proceeds in USD"]), None),
            ("sales_usd", _num(row["Sales in USD"]), None)]


REPORTS: dict[str, Callable[[dict], list[tuple[str, float, float | None]]]] = {
    "App Downloads Standard": _downloads,
    "App Store Discovery and Engagement Standard": _discovery,
    "App Store Installation and Deletion Standard": _installs,
    "App Store Purchases Standard": _purchases,
}


def _num(text: str) -> float:
    text = (text or "").strip().replace(",", "")
    return float(text) if text else 0.0


Totals = dict[tuple[str, str, str, str], list]  # (date, territory, device, metric) -> [value, uniques]


def new_totals() -> Totals:
    return defaultdict(lambda: [0.0, None])


def add_report(name: str, data: bytes, totals: Totals) -> Totals:
    """Add one gzipped report file's rows into `totals`."""
    mapper = REPORTS[name]
    text = gzip.decompress(data).decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        for metric, value, uniques in mapper(row):
            entry = totals[(row["Date"], row["Territory"], row["Device"], metric)]
            entry[0] += value
            if uniques is not None:
                entry[1] = (entry[1] or 0.0) + uniques
    return totals


SERVER_ERROR_WAIT = (5, 20)  # seconds before the 2nd and 3rd try after an Apple 5xx


class Client:
    def __init__(self, issuer_id: str, key_id: str, private_key: str, transport: httpx.BaseTransport | None = None):
        self._issuer_id, self._key_id, self._key = issuer_id, key_id, private_key
        self._token, self._token_exp = "", 0.0
        self._api = httpx.Client(base_url=API, timeout=60, transport=transport)
        # Report files come from presigned storage links that must not get our token.
        self._files = httpx.Client(timeout=120, transport=transport)

    def _bearer(self) -> str:
        # Apple allows tokens of at most 20 minutes; renew a few minutes early.
        if time.time() > self._token_exp - 180:
            issued = int(time.time())
            self._token_exp = issued + 1200
            self._token = jwt.encode(
                {"iss": self._issuer_id, "iat": issued, "exp": self._token_exp, "aud": "appstoreconnect-v1"},
                self._key, algorithm="ES256", headers={"kid": self._key_id, "typ": "JWT"})
        return self._token

    def _request(self, path: str, params: dict | None = None, allow_missing: bool = False) -> httpx.Response | None:
        for attempt in range(3):
            r = self._api.get(path, params=params, headers={"Authorization": f"Bearer {self._bearer()}"})
            if r.status_code == 429 and attempt < 2:
                time.sleep(min(int(r.headers.get("Retry-After", "10")), 60))
                continue
            if r.status_code >= 500 and attempt < 2:
                time.sleep(SERVER_ERROR_WAIT[attempt])  # Apple's own hiccups often pass in seconds
                continue
            if r.status_code >= 500:
                raise AscError(f"App Store Connect had a server error ({r.status_code}) for {path}. "
                               "That's on Apple's side; the hub tries again at its next pull.")
            if r.status_code == 404 and allow_missing:
                return None
            if r.status_code == 401:
                raise AscError("Apple refused the hub's App Store Connect key (revoked or wrong key ID).")
            if r.status_code == 403:
                raise AscError(f"The hub's App Store Connect key isn't allowed to read {path}.")
            if r.status_code >= 400:
                raise AscError(f"App Store Connect answered {r.status_code} for {path}.")
            return r
        raise AscError("App Store Connect kept saying to slow down (rate limit).")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request(path, params).json()

    def sales_report(self, vendor_number: str, day: str) -> bytes | None:
        """One day's gzipped Sales and Trends summary, or None when Apple has none
        (no sales that day, or not published yet)."""
        r = self._request("/v1/salesReports", {
            "filter[frequency]": "DAILY", "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY",
            "filter[vendorNumber]": vendor_number, "filter[reportDate]": day, "filter[version]": "1_1",
        }, allow_missing=True)
        return r.content if r is not None else None

    def paged(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = self.get(path, params)
        while True:
            yield from page.get("data", [])
            nxt = page.get("links", {}).get("next")
            if not nxt:
                return
            page = self.get(nxt.removeprefix(API))

    def download(self, url: str) -> bytes:
        r = self._files.get(url)
        if r.status_code >= 400:
            raise AscError(f"Couldn't download an Apple report file ({r.status_code}).")
        return r.content


def ongoing_request_id(client: Client, app_id: str) -> str:
    stopped = False
    for req in client.paged(f"/v1/apps/{app_id}/analyticsReportRequests"):
        attrs = req["attributes"]
        if attrs["accessType"] == "ONGOING":
            if not attrs.get("stoppedDueToInactivity"):
                return req["id"]
            stopped = True
    if stopped:
        # Apple stops a request nobody reads; only a new request starts the files again.
        raise AscError(f"Apple stopped the analytics reports for app {app_id} (unused too long). "
                       "A new report request is needed to start them again.")
    raise NoReportRequest(f"App {app_id} has no active ongoing analytics report request in App Store Connect.")


def pull_app(conn: sqlite3.Connection, client: Client, app_id: str, today: dt.date | None = None) -> int:
    """Read every daily file not yet saved, plus the last few days again. Returns files read."""
    today = today or dt.date.today()
    refresh_from = (today - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    request_id = ongoing_request_id(client, app_id)
    reports = {r["attributes"]["name"]: r["id"]
               for r in client.paged(f"/v1/analyticsReportRequests/{request_id}/reports", {"limit": 200})}
    missing = [name for name in REPORTS if name not in reports]
    if missing:
        raise AscError(f"App {app_id} is missing Apple reports: {', '.join(missing)}.")

    saved = {(r["report"], r["processing_date"]) for r in conn.execute(
        "SELECT report, processing_date FROM asc_instances WHERE app_id = ?", (app_id,))}
    read = 0
    for name in REPORTS:
        instances = client.paged(f"/v1/analyticsReports/{reports[name]}/instances",
                                 {"filter[granularity]": "DAILY", "limit": 200})
        for inst in instances:
            day = inst["attributes"]["processingDate"]
            if (name, day) in saved and day < refresh_from:
                continue
            totals = new_totals()
            for seg in client.paged(f"/v1/analyticsReportInstances/{inst['id']}/segments"):
                add_report(name, client.download(seg["attributes"]["url"]), totals)
            save_instance(conn, app_id, name, day, inst["id"], totals)
            read += 1
    return read


def save_instance(conn: sqlite3.Connection, app_id: str, report: str, processing_date: str, instance_id: str,
                  totals: Totals) -> None:
    """Replace one file's rows, all or nothing."""
    with conn:
        conn.execute("DELETE FROM asc_rows WHERE app_id = ? AND report = ? AND processing_date = ?",
                     (app_id, report, processing_date))
        conn.executemany(
            "INSERT INTO asc_rows (app_id, report, processing_date, date, territory, device, metric, value, uniques) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(app_id, report, processing_date, d, t, dev, m, v, u) for (d, t, dev, m), (v, u) in totals.items()])
        conn.execute("INSERT OR REPLACE INTO asc_instances VALUES (?, ?, ?, ?, ?)",
                     (app_id, report, processing_date, instance_id, db.now()))


# Apple's crash counts by app version, device and OS version. Apple fills this
# report only from users who share analytics with developers, and only once at
# least five of them hit crashes; a day can still grow for five days.
CRASH_REPORT = "App Crashes"


def parse_crashes(data: bytes) -> dict[tuple[str, str, str, str], list[float]]:
    """A gzipped App Crashes file -> {(date, app version, device, OS version): [crashes, devices]}."""
    totals: dict[tuple[str, str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    text = gzip.decompress(data).decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        entry = totals[(row["Date"], row["App Version"], row["Device"], row["Platform Version"])]
        entry[0] += _num(row["Crashes"])
        entry[1] += _num(row["Unique Devices"])
    return dict(totals)


def pull_crashes(conn: sqlite3.Connection, client: Client, app_id: str, today: dt.date | None = None) -> int:
    """Read every App Crashes daily file not yet saved, plus the last few days
    again. Returns files read. Apple writes a file only for days it has crashes
    to show, so none at all is normal for a small app."""
    today = today or dt.date.today()
    refresh_from = (today - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    request_id = ongoing_request_id(client, app_id)
    report_id = next((r["id"] for r in client.paged(f"/v1/analyticsReportRequests/{request_id}/reports",
                                                    {"limit": 200})
                      if r["attributes"]["name"] == CRASH_REPORT), None)
    if report_id is None:
        raise AscError(f"App {app_id} has no {CRASH_REPORT} report in its analytics report request.")
    saved = {r["processing_date"] for r in conn.execute(
        "SELECT processing_date FROM asc_instances WHERE app_id = ? AND report = ?", (app_id, CRASH_REPORT))}
    read = 0
    for inst in client.paged(f"/v1/analyticsReports/{report_id}/instances",
                             {"filter[granularity]": "DAILY", "limit": 200}):
        day = inst["attributes"]["processingDate"]
        if day in saved and day < refresh_from:
            continue
        totals: dict[tuple[str, str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for seg in client.paged(f"/v1/analyticsReportInstances/{inst['id']}/segments"):
            for key, (crashes, devices) in parse_crashes(client.download(seg["attributes"]["url"])).items():
                totals[key][0] += crashes
                totals[key][1] += devices
        save_crashes(conn, app_id, day, inst["id"], totals)
        read += 1
    return read


def save_crashes(conn: sqlite3.Connection, app_id: str, processing_date: str, instance_id: str,
                 totals: dict[tuple[str, str, str, str], list[float]]) -> None:
    """Replace one App Crashes file's rows, all or nothing."""
    with conn:
        conn.execute("DELETE FROM apple_crash_rows WHERE app_id = ? AND processing_date = ?",
                     (app_id, processing_date))
        conn.executemany("INSERT INTO apple_crash_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [(app_id, processing_date, d, v, dev, os, c, n)
                          for (d, v, dev, os), (c, n) in totals.items()])
        conn.execute("INSERT OR REPLACE INTO asc_instances VALUES (?, ?, ?, ?, ?)",
                     (app_id, CRASH_REPORT, processing_date, instance_id, db.now()))


# Sales and Trends product types: first-time downloads and repeat downloads
# (iPhone, universal, iPad, Mac and the Apple Arcade/education variants).
# Updates (7, 7F, ...) and in-app purchases (IA1, ...) are left out.
FIRST_TIME_TYPES = {"1", "1F", "1T", "F1", "1E", "1EP", "1EU"}
REDOWNLOAD_TYPES = {"3", "3F", "3T", "F3"}
# Apple keeps a year of daily sales reports.
SALES_HISTORY_DAYS = 365


def parse_sales(data: bytes) -> dict[tuple[str, str, str, str], float]:
    """A gzipped daily sales summary -> {(app_id, territory, device, metric): units}."""
    totals: dict[tuple[str, str, str, str], float] = defaultdict(float)
    text = gzip.decompress(data).decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        kind = row["Product Type Identifier"]
        metric = ("first_time_units" if kind in FIRST_TIME_TYPES
                  else "redownload_units" if kind in REDOWNLOAD_TYPES else None)
        if metric:
            totals[(row["Apple Identifier"], row["Country Code"], row["Device"], metric)] += _num(row["Units"])
    return dict(totals)


def pull_sales(conn: sqlite3.Connection, client: Client, vendor_number: str, today: dt.date | None = None) -> int:
    """Read every daily sales report of the past year not yet saved, plus the
    last few days again. Keeps every app on the account. Returns days read."""
    today = today or dt.date.today()
    refresh_from = (today - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    saved = {r["date"] for r in conn.execute("SELECT date FROM sales_days")}
    read = 0
    for back in range(SALES_HISTORY_DAYS, 0, -1):
        day = (today - dt.timedelta(days=back)).isoformat()
        if day in saved and day < refresh_from:
            continue
        data = client.sales_report(vendor_number, day)
        with conn:
            if data is not None:
                conn.execute("DELETE FROM sales_rows WHERE date = ?", (day,))
                conn.executemany("INSERT INTO sales_rows VALUES (?, ?, ?, ?, ?, ?)",
                                 [(day, a, t, d, m, v) for (a, t, d, m), v in parse_sales(data).items()])
            conn.execute("INSERT OR REPLACE INTO sales_days VALUES (?, ?, ?)", (day, db.now(), int(data is not None)))
        read += 1
    return read
