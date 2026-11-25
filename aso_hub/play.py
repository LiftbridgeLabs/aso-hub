"""Google Play: crashes and ANRs ("app not responding") from the Play Developer
Reporting API, then the report files (play_reports.py).

The hub reads with its own service account (your service account),
which Play Console grants read-only access. Google computes no daily crash
rate for an app with few users, so the hub reads the crash and ANR issues
themselves: each one with its report count, users, app version and a link to
it in Play Console.
"""
import datetime as dt
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

import httpx
import jwt

from . import db
from .settings import Settings

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an address)
REPORTING = "https://playdeveloperreporting.googleapis.com/v1beta1"
REPORTING_SCOPE = "https://www.googleapis.com/auth/playdeveloperreporting"
# How far back the hub asks for crashes and ANRs. With no interval Google
# answers for only the most recent stretch and misses older ones.
ISSUE_DAYS = 30
# The search filter's names; the issues themselves call an ANR APPLICATION_NOT_RESPONDING.
ISSUE_FILTER = "errorIssueType = CRASH OR errorIssueType = ANR"
ANR_TYPES = {"ANR", "APPLICATION_NOT_RESPONDING"}


class PlayError(Exception):
    """A problem worth showing on the page, in plain words."""


class Client:
    def __init__(self, key: dict, transport: httpx.BaseTransport | None = None):
        self._key = key
        self._http = httpx.Client(timeout=60, transport=transport)
        self._tokens: dict[str, tuple[str, float]] = {}

    def _token(self, scope: str) -> str:
        token, expires = self._tokens.get(scope, ("", 0.0))
        if time.time() < expires - 180:
            return token
        now = int(time.time())
        assertion = jwt.encode({"iss": self._key["client_email"], "scope": scope, "aud": TOKEN_URL,
                                "iat": now, "exp": now + 3600}, self._key["private_key"], algorithm="RS256")
        r = self._http.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                             "assertion": assertion})
        if r.status_code != 200:
            raise PlayError("Google refused the hub's service account key (deleted or disabled?). "
                            "Make a new key for the service account in Google Cloud.")
        body = r.json()
        self._tokens[scope] = (body["access_token"], now + body.get("expires_in", 3600))
        return body["access_token"]

    def get(self, url: str, scope: str, params: dict | None = None) -> dict:
        return self._get(url, scope, params).json()

    def get_bytes(self, url: str, scope: str, params: dict | None = None) -> bytes:
        return self._get(url, scope, params).content

    def post(self, url: str, scope: str, body: dict) -> dict:
        r = self._http.post(url, json=body, headers={"Authorization": f"Bearer {self._token(scope)}"})
        return self._check(r).json()

    def _get(self, url: str, scope: str, params: dict | None) -> httpx.Response:
        r = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token(scope)}"})
        return self._check(r)

    @staticmethod
    def _check(r: httpx.Response) -> httpx.Response:
        if r.status_code in (401, 403):
            raise PlayError("Google Play refused the hub's service account. Check that "
                            "The service account is still a user in Play Console.")
        if r.status_code >= 400:
            raise PlayError(f"Google Play answered {r.status_code}.")
        return r


def _interval(now: dt.datetime, days: int) -> dict:
    """The last `days` days in UTC, on whole hours, as Google's query parameters."""
    end = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return _interval_between(end - dt.timedelta(days=days), end)


def _interval_between(start: dt.datetime, end: dt.datetime) -> dict:
    """A UTC window on whole hours as Google's query parameters (issue search takes UTC only)."""
    params = {}
    for name, when in (("startTime", start.astimezone(dt.UTC)), ("endTime", end.astimezone(dt.UTC))):
        params |= {f"interval.{name}.year": when.year, f"interval.{name}.month": when.month,
                   f"interval.{name}.day": when.day, f"interval.{name}.hours": when.hour,
                   f"interval.{name}.timeZone.id": "UTC"}
    return params


def error_issues(client: Client, package: str, now: dt.datetime | None = None) -> list[dict]:
    """Crash and ANR issues from the last ISSUE_DAYS days; counts cover that window."""
    window = _interval(now or dt.datetime.now(dt.UTC), ISSUE_DAYS)
    issues, page = [], None
    while True:
        params = {"pageSize": 50, "filter": ISSUE_FILTER} | window | ({"pageToken": page} if page else {})
        body = client.get(f"{REPORTING}/apps/{package}/errorIssues:search", REPORTING_SCOPE, params)
        issues += body.get("errorIssues", [])
        page = body.get("nextPageToken")
        if not page:
            return issues


# Google's daily error counts run on Pacific days.
PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), PACIFIC)


def counts_end(client: Client, package: str) -> dt.date:
    """The first Pacific day Google has no complete daily error counts for yet."""
    body = client.get(f"{REPORTING}/apps/{package}/errorCountMetricSet", REPORTING_SCOPE)
    for f in body.get("freshnessInfo", {}).get("freshnesses", []):
        if f.get("aggregationPeriod") == "DAILY":
            end = f["latestEndTime"]
            return dt.date(end["year"], end["month"], end["day"])
    raise PlayError(f"Google Play gave no daily crash counts for {package}.")


def error_counts(client: Client, package: str, start: dt.date, end: dt.date) -> list[dict]:
    """Crash and ANR report counts per Pacific day and version code, from `start` up to
    (not including) `end`. Unlike crash rates, these come back for small apps too."""
    def day(d: dt.date) -> dict:
        return {"year": d.year, "month": d.month, "day": d.day, "timeZone": {"id": "America/Los_Angeles"}}

    out, page = [], None
    while True:
        body = client.post(f"{REPORTING}/apps/{package}/errorCountMetricSet:query", REPORTING_SCOPE, {
            "timelineSpec": {"aggregationPeriod": "DAILY", "startTime": day(start), "endTime": day(end)},
            "dimensions": ["reportType", "versionCode"], "metrics": ["errorReportCount"], "pageSize": 1000,
        } | ({"pageToken": page} if page else {}))
        for row in body.get("rows", []):
            dims = {d["dimension"]: d.get("stringValue") or d.get("int64Value") for d in row["dimensions"]}
            count = int(float(row["metrics"][0].get("decimalValue", {}).get("value") or 0))
            when = row["startTime"]
            # Only crashes and freezes; Google also counts non-fatal reports here.
            if count and (dims.get("reportType") == "CRASH" or dims.get("reportType") in ANR_TYPES):
                out.append({"date": dt.date(when["year"], when["month"], when["day"]).isoformat(),
                            "type": dims.get("reportType"), "version_code": dims.get("versionCode") or "?",
                            "count": count})
        page = body.get("nextPageToken")
        if not page:
            return out


def week_issues(client: Client, package: str, start: dt.date, end: dt.date) -> list[dict]:
    """Crash and ANR issues with reports between Pacific midnights `start` and `end`;
    their counts and users cover only that window."""
    window = _interval_between(_pacific_midnight(start), _pacific_midnight(end))
    issues, page = [], None
    while True:
        params = {"pageSize": 50, "filter": ISSUE_FILTER} | window | ({"pageToken": page} if page else {})
        body = client.get(f"{REPORTING}/apps/{package}/errorIssues:search", REPORTING_SCOPE, params)
        issues += body.get("errorIssues", [])
        page = body.get("nextPageToken")
        if not page:
            return issues


def pull(conn: sqlite3.Connection, settings: Settings, client: Client | None = None,
         today: dt.date | None = None) -> str:
    client = client or client_for(settings)
    day = (today or dt.datetime.now(dt.UTC).date()).isoformat()
    apps = [a for a in settings.apps if a.store == "google_play"]
    # One app's refusal (not in this Play account, say) doesn't stop the others.
    found, failed = {}, []
    for a in apps:
        try:
            found[a.slug] = error_issues(client, a.google_package)
        except PlayError as e:
            failed.append((a, e))
    if failed and not found:
        raise failed[0][1]  # every app refused: a key or permission problem, in its own words
    with conn:
        for slug, issues in found.items():
            db.save_snapshot(conn, day, "play_issues", slug, {"issues": issues})
    count = sum(len(i) for i in found.values())
    message = f"read {count} crash and ANR issue{'' if count == 1 else 's'}"
    if settings.play_bucket:
        from . import play_reports  # imports this module's Client

        message += "; " + play_reports.pull(conn, settings, client)
    if failed:
        raise PlayError("; ".join(f"{a.label}: {e}" for a, e in failed))
    return message


def client_for(settings: Settings) -> Client:
    key = settings.read_secret(settings.google_key_path)
    if not key:
        raise PlayError("Google Play isn't set up (GOOGLE_KEY_PATH).")
    return Client(json.loads(key))


def issue_label(issue: dict) -> str:
    return "ANR (app froze)" if issue.get("type") in ANR_TYPES else "Crash"
