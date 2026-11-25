"""The weekly crash digest: one Discord message per app, to that app's own #alerts.

Each message covers the last full week (Monday to Sunday) against the week
before: Apple crashes by app version (the App Crashes analytics report), Google
Play crashes and freezes (ANRs) by version code, and the week's top Android
issues with a link to each in Play Console. A week with none says so, so a
missing message means the job didn't run.

Each app's webhook is a file, alerts-<app> (alerts-lumen), in the hub's
secrets folder; only apps listed under [crash_digest] in config.toml post. If a
pull or a post fails, a message goes to alerts-admin (a channel for failures,
where CI failures go) and the run exits non-zero. Webhook URLs are never
printed, not even in an error.
"""
import datetime as dt
import sqlite3
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from . import asc, db, notify, play
from .settings import App, Settings

LIMIT = notify.LIMIT
ISSUE_CHARS = 90
APPLE_NOTE = ("Apple only reports a crash from people who share analytics with developers, and only once "
              "at least 5 of them crash, so a small app usually shows none. Its days can still grow for 5 days.")
APPLE_WHERE = "Details: Xcode > Window > Organizer > Crashes."


class DigestError(Exception):
    pass


@dataclass
class Week:
    start: dt.date  # a Monday

    @property
    def end(self) -> dt.date:  # the Monday after; not included
        return self.start + dt.timedelta(days=7)

    @property
    def before(self) -> "Week":
        return Week(self.start - dt.timedelta(days=7))

    @property
    def label(self) -> str:
        last = self.end - dt.timedelta(days=1)
        first = f"{self.start:%b} {self.start.day}"
        return f"{first}–{last.day}" if last.month == self.start.month else f"{first}–{last:%b} {last.day}"


def last_full_week(today: dt.date) -> Week:
    return Week(today - dt.timedelta(days=today.weekday() + 7))


@dataclass
class Section:
    """One store's part of an app's digest."""
    title: str
    lines: list[str] = field(default_factory=list)
    error: str | None = None


PLURALS = {"crash": "crashes", "freeze": "freezes", "report": "reports", "person": "people"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word if n == 1 else PLURALS[word]}"


def _kinds(crashes: int, freezes: int | None = None) -> str:
    """'3 crashes, 1 freeze', or 'none'."""
    parts = [_plural(crashes, "crash")] if crashes else []
    if freezes:
        parts.append(_plural(freezes, "freeze"))
    return ", ".join(parts) or "none"


def apple_section(conn: sqlite3.Connection, app: App, week: Week, pull_error: str | None = None) -> Section:
    section = Section("iOS (Apple)")
    if pull_error:
        section.error = pull_error
        section.lines.append(f"Couldn't read Apple's crash report this week: {pull_error}")
        return section

    def by_version(w: Week) -> dict[str, int]:
        rows = conn.execute("SELECT app_version, SUM(crashes) AS n FROM apple_crashes_current "
                            "WHERE app_id = ? AND date >= ? AND date < ? GROUP BY app_version",
                            (app.apple_id, w.start.isoformat(), w.end.isoformat()))
        return {r["app_version"]: int(r["n"]) for r in rows if r["n"]}

    now, before = by_version(week), by_version(week.before)
    total, total_before = sum(now.values()), sum(before.values())
    if not total:
        section.lines.append(f"No crashes reported (week before: {_kinds(total_before)}).")
    else:
        section.lines.append(f"{_plural(total, 'crash')} (week before: {_kinds(total_before)}).")
        versions = sorted(now.items(), key=lambda kv: -kv[1])
        section.lines.append("By version: " + " · ".join(f"{v}: {n}" for v, n in versions))
        top = conn.execute("SELECT app_version, device, platform_version, SUM(crashes) AS n "
                           "FROM apple_crashes_current WHERE app_id = ? AND date >= ? AND date < ? "
                           "GROUP BY app_version, device, platform_version ORDER BY n DESC LIMIT 3",
                           (app.apple_id, week.start.isoformat(), week.end.isoformat())).fetchall()
        section.lines.append("Most: " + " · ".join(
            f"{r['app_version']} on {r['device']} {r['platform_version']} ({int(r['n'])})" for r in top))
        section.lines.append(APPLE_WHERE)
    section.lines.append(f"-# {APPLE_NOTE}")
    return section


def _issue_line(issue: dict) -> str:
    what = " in ".join(p for p in (issue.get("location"), issue.get("cause")) if p) or "no details"
    if len(what) > ISSUE_CHARS:
        what = what[:ISSUE_CHARS - 1].rstrip() + "…"
    reports, users = int(issue.get("errorReportCount") or 0), int(issue.get("distinctUsers") or 0)
    version = (issue.get("lastAppVersion") or {}).get("versionCode", "?")
    kind = "Freeze" if issue.get("type") in play.ANR_TYPES else "Crash"
    line = f"• {kind}: {what} · {_plural(reports, 'report')}, {_plural(users, 'person')}, code {version}"
    uri = issue.get("issueUri") or ""
    return line + (f"\n  <{uri}>" if uri.startswith("https://play.google.com/") else "")


def google_section(client: play.Client, app: App, week: Week, top: int) -> Section:
    section = Section("Android (Google Play)")
    ready = play.counts_end(client, app.google_package)
    end = min(week.end, ready)
    if end <= week.start:
        raise play.PlayError(f"Google has no crash counts for {app.label} past {ready - dt.timedelta(days=1)} yet.")
    counts = play.error_counts(client, app.google_package, week.before.start, end)
    now: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # version code -> [crashes, freezes]
    before = [0, 0]
    for c in counts:
        slot = 1 if c["type"] in play.ANR_TYPES else 0
        if c["date"] >= week.start.isoformat():
            now[c["version_code"]][slot] += c["count"]
        else:
            before[slot] += c["count"]
    crashes, freezes = sum(v[0] for v in now.values()), sum(v[1] for v in now.values())
    last = end - dt.timedelta(days=1)
    upto = "" if end == week.end else f" (Google's numbers run to {last:%b} {last.day})"
    if not crashes and not freezes:
        section.lines.append(f"No crashes or freezes reported{upto} (week before: {_kinds(*before)}).")
        return section
    section.lines.append(f"{_kinds(crashes, freezes)}{upto} (week before: {_kinds(*before)}).")
    section.lines.append("By version: " + " · ".join(
        f"code {v}: {_kinds(*n)}" for v, n in sorted(now.items(), key=lambda kv: -sum(kv[1]))))
    issues = play.week_issues(client, app.google_package, week.start, end)
    issues.sort(key=lambda i: -int(i.get("errorReportCount") or 0))
    if issues:
        section.lines.append("Top issues:")
        section.lines += [_issue_line(i) for i in issues[:top]]
    return section


def message(name: str, week: Week, sections: list[Section]) -> str:
    def render() -> str:
        return "\n".join([f"**{name}: crashes for {week.label}**",
                          *(f"**{s.title}:** " + "\n".join(s.lines) for s in sections)])

    text = render()
    while len(text) > LIMIT:
        # Too long for Discord: drop issues from the end, then cut as a last resort.
        with_issues = [s for s in sections if any(line.startswith("•") for line in s.lines)]
        if not with_issues:
            return text[:LIMIT]
        lines = with_issues[-1].lines
        del lines[max(i for i, line in enumerate(lines) if line.startswith("•"))]
        text = render()
    return text


def _webhook(settings: Settings, name: str) -> str | None:
    url = settings.read_secret(settings.alerts_webhook_file(name))
    if url and not url.startswith(notify.WEBHOOK_PREFIXES):
        raise DigestError(f"The saved #alerts webhook for {name} isn't a Discord webhook address.")
    return url


def run(settings: Settings, conn: sqlite3.Connection, today: dt.date | None = None, dry_run: bool = False,
        again: bool = False, asc_client: asc.Client | None = None, play_client: play.Client | None = None,
        transport: httpx.BaseTransport | None = None) -> bool:
    """Post this week's digests. Returns False if anything failed (and says so in #alerts)."""
    week = last_full_week(today or dt.date.today())
    wanted = settings.crash_digest.get("apps", [])
    top = int(settings.crash_digest.get("top_issues", 3))
    known = {a.name for a in settings.apps}
    failures = [f"{n} is listed under [crash_digest] but isn't an app in config.toml." for n in wanted
                if n not in known]
    run_id = db.start_run(conn, "crash_digest")
    posted = []
    with httpx.Client(transport=transport, timeout=20, headers={"User-Agent": "aso-hub"}) as http:
        for name in (n for n in wanted if n in known):
            sent = conn.execute("SELECT 1 FROM crash_digests WHERE app = ? AND week_start = ?",
                                (name, week.start.isoformat())).fetchone()
            if sent and not again and not dry_run:
                print(f"{name}: the digest for {week.label} was already posted")
                continue
            try:
                url = None if dry_run else _webhook(settings, name)
                if not dry_run and not url:
                    path = settings.alerts_webhook_file(name)
                    raise DigestError(f"no #alerts webhook saved ({path.name})" if path
                                      else "ALERTS_WEBHOOK_DIR isn't set")
                sections = []
                for app in (a for a in settings.apps if a.name == name):
                    if app.store == "app_store":
                        sections.append(_apple(settings, conn, app, week, asc_client))
                    else:
                        sections.append(_google(settings, app, week, top, play_client))
                text = message(name, week, sections)
                failures += [f"{name}, {s.title}: {s.error}" for s in sections if s.error]
                if dry_run:
                    print(text, end="\n\n")
                    continue
                notify._post(http, url, text)
                with conn:
                    conn.execute("INSERT OR REPLACE INTO crash_digests VALUES (?, ?, ?)",
                                 (name, week.start.isoformat(), db.now()))
                posted.append(name)
                print(f"{name}: posted the digest for {week.label}")
            except (DigestError, notify.NotifyError) as e:
                failures.append(f"{name}: {e}")
            except Exception as e:  # anything unexpected still reaches alerts-admin and the other apps
                # Only the kind in the message: error text can carry a presigned link or a webhook.
                failures.append(f"{name}: unexpected error ({type(e).__name__})")
                traceback.print_exc()
        summary = f"posted {', '.join(posted) or 'nothing'} for {week.label}"
        if failures:
            _alert_failure(settings, http, week, failures, posted, dry_run)
    db.finish_run(conn, run_id, not failures, summary + (f"; {len(failures)} problem(s)" if failures else ""))
    print(summary)
    return not failures


def _apple(settings: Settings, conn: sqlite3.Connection, app: App, week: Week, client: asc.Client | None) -> Section:
    try:
        if client is None:
            key = settings.read_secret(settings.asc_key_path)
            if not (settings.asc_issuer_id and settings.asc_key_id and key):
                raise asc.AscError("App Store Connect isn't set up (ASC_ISSUER_ID, ASC_KEY_ID, ASC_KEY_PATH).")
            client = asc.Client(settings.asc_issuer_id, settings.asc_key_id, key)
        asc.pull_crashes(conn, client, app.apple_id)
    except asc.NoReportRequest:
        # A new app: nothing failed, Apple just has no reports for it yet.
        return Section("iOS (Apple)", ["No Apple crash numbers yet: the app has no analytics report request "
                                       "(a one-time step with an Admin key)."])
    except asc.AscError as e:
        return apple_section(conn, app, week, pull_error=str(e))
    except httpx.HTTPError as e:
        # Network errors can carry a report file's presigned link; keep only the kind.
        return apple_section(conn, app, week, pull_error=f"couldn't reach Apple ({type(e).__name__})")
    return apple_section(conn, app, week)


def _google(settings: Settings, app: App, week: Week, top: int, client: play.Client | None) -> Section:
    try:
        return google_section(client or play.client_for(settings), app, week, top)
    except play.PlayError as e:
        reason = str(e)
    except httpx.HTTPError as e:
        reason = f"couldn't reach Google ({type(e).__name__})"
    return Section("Android (Google Play)", [f"Couldn't read Google's crash numbers this week: {reason}"], reason)


def _alert_failure(settings: Settings, http: httpx.Client, week: Week, failures: list[str], posted: list[str],
                   dry_run: bool) -> None:
    lines = [f"**ASO Hub crash digest for {week.label}: something failed**", *(f"• {f}" for f in failures)]
    lines.append(f"Posted: {', '.join(posted)}." if posted else "No digest posted.")
    text = "\n".join(lines)[:LIMIT]
    print(text, file=sys.stderr)
    if dry_run:
        return
    try:
        url = _webhook(settings, "admin")
        if not url:
            print("no alerts-admin webhook saved; the failure above went nowhere else", file=sys.stderr)
            return
        notify._post(http, url, text)
    except (DigestError, notify.NotifyError) as e:
        print(f"couldn't post the failure to #alerts either: {e}", file=sys.stderr)
