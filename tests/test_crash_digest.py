import dataclasses
import datetime as dt
import json

import httpx
import pytest

from aso_hub import asc, crash_digest, db
from conftest import tsv

CRASH_HEADER = ["Date", "App Name", "App Apple Identifier", "App Version", "Device", "Platform Version", "Crashes",
                "Unique Devices"]
TODAY = dt.date(2026, 11, 23)  # a Monday: the digest covers Nov 16-22, against Nov 9-15
LUMEN_HOOK = "https://discord.com/api/webhooks/1/lumen-secret"
ADMIN_HOOK = "https://discord.com/api/webhooks/2/admin-secret"


def crash_row(date, version, count, device="iPhone", os="iOS 27.2"):
    return [date, "Lumen", "1000000001", version, device, os, count, count]


class FakeAsc:
    """Answers like App Store Connect for one app whose App Crashes report has `files`."""

    def __init__(self, files: dict[str, bytes], fail: bool = False):
        self.files, self.fail = files, fail

    def paged(self, path, params=None):
        if self.fail:
            raise asc.AscError("App Store Connect had a server error (500).")
        if path.endswith("/analyticsReportRequests"):
            yield {"id": "req", "attributes": {"accessType": "ONGOING"}}
        elif path.endswith("/reports"):
            yield from ({"id": "other", "attributes": {"name": "App Sessions Standard"}},
                        {"id": "rep", "attributes": {"name": "App Crashes"}})
        elif path.endswith("/instances"):
            yield from ({"id": f"i-{d}", "attributes": {"processingDate": d}} for d in self.files)
        elif path.endswith("/segments"):
            yield {"attributes": {"url": path.split("/")[-2].removeprefix("i-")}}

    def download(self, url):
        return self.files[url]


def count_row(day: str, kind: str, code: str, n: int) -> dict:
    y, m, d = map(int, day.split("-"))
    return {"startTime": {"year": y, "month": m, "day": d},
            "dimensions": [{"dimension": "reportType", "stringValue": kind},
                           {"dimension": "versionCode", "stringValue": code}],
            "metrics": [{"metric": "errorReportCount", "decimalValue": {"value": str(n)}}]}


class FakePlay:
    def __init__(self, rows, issues, fresh=(2026, 11, 23)):
        self.rows, self.issues, self.fresh = rows, issues, fresh
        self.issue_params = None

    def get(self, url, scope, params=None):
        if url.endswith("/errorCountMetricSet"):
            y, m, d = self.fresh
            return {"freshnessInfo": {"freshnesses": [
                {"aggregationPeriod": "HOURLY", "latestEndTime": {"year": 2030, "month": 1, "day": 1}},
                {"aggregationPeriod": "DAILY", "latestEndTime": {"year": y, "month": m, "day": d}}]}}
        self.issue_params = params
        return {"errorIssues": self.issues}

    def post(self, url, scope, body):
        end = body["timelineSpec"]["endTime"]
        end = dt.date(end["year"], end["month"], end["day"]).isoformat()
        return {"rows": [r for r in self.rows if "{year}-{month:02}-{day:02}".format(**r["startTime"]) < end]}


ISSUE = {"name": "apps/x/errorIssues/abc", "type": "CRASH", "cause": "com.x.Player.seek",
         "location": "java.lang.IndexOutOfBoundsException", "errorReportCount": "3", "distinctUsers": "2",
         "lastAppVersion": {"versionCode": "28"},
         "issueUri": "https://play.google.com/console/developers/1/app/2/vitals/crashes/abc/details"}


class FakeDiscord:
    def __init__(self, status=200):
        self.posts: list[tuple[str, str]] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((str(request.url.copy_with(query=None)), json.loads(request.content)["content"]))
        return httpx.Response(self.status, json={"id": "1"})


@pytest.fixture
def digest_cfg(cfg, tmp_path):
    (tmp_path / "alerts-lumen").write_text(LUMEN_HOOK + "\n")
    (tmp_path / "alerts-admin").write_text(ADMIN_HOOK + "\n")
    return dataclasses.replace(cfg, alerts_webhook_dir=tmp_path, crash_digest={"apps": ["Lumen"], "top_issues": 3})


def test_week_is_the_last_full_monday_to_sunday():
    week = crash_digest.last_full_week(dt.date(2026, 11, 18))  # a Wednesday
    assert (week.start, week.end, week.label) == (dt.date(2026, 11, 9), dt.date(2026, 11, 16), "Nov 9–15")
    assert crash_digest.last_full_week(TODAY).start == dt.date(2026, 11, 16)
    assert crash_digest.Week(dt.date(2026, 11, 30)).label == "Nov 30–Dec 6"


def test_apple_crashes_newest_file_wins(conn, cfg):
    app = cfg.app("lumen-ios")
    client = FakeAsc({
        "2026-11-18": tsv(CRASH_HEADER, [crash_row("2026-11-17", "1.14", 5)]),
        # The next file restates Nov 17 with a late crash, and adds Nov 18.
        "2026-11-19": tsv(CRASH_HEADER, [crash_row("2026-11-17", "1.14", 6),
                                         crash_row("2026-11-18", "1.13", 2, device="iPad", os="iPadOS 27.1")]),
        "2026-11-12": tsv(CRASH_HEADER, [crash_row("2026-11-11", "1.13", 4)]),
    })
    assert asc.pull_crashes(conn, client, app.apple_id, today=TODAY) == 3
    section = crash_digest.apple_section(conn, app, crash_digest.last_full_week(TODAY))
    assert section.lines[0] == "8 crashes (week before: 4 crashes)."
    assert section.lines[1] == "By version: 1.14: 6 · 1.13: 2"
    assert section.lines[2] == "Most: 1.14 on iPhone iOS 27.2 (6) · 1.13 on iPad iPadOS 27.1 (2)"
    assert "Organizer" in section.lines[3] and section.lines[-1].startswith("-# Apple only reports")

    # Old files are not read again; recent ones are (Apple can still add to them).
    assert asc.pull_crashes(conn, client, app.apple_id, today=TODAY) == 2


def test_no_apple_crashes_says_so(conn, cfg):
    section = crash_digest.apple_section(conn, cfg.app("lumen-ios"), crash_digest.last_full_week(TODAY))
    assert section.lines[0] == "No crashes reported (week before: none)."


def test_google_counts_by_version_with_top_issues(cfg):
    client = FakePlay([
        count_row("2026-11-16", "CRASH", "28", 2), count_row("2026-11-20", "CRASH", "28", 1),
        count_row("2026-11-21", "ANR", "27", 1),
        count_row("2026-11-10", "CRASH", "27", 4),  # the week before
        count_row("2026-11-18", "NON_FATAL", "28", 9),  # not a crash
    ], [dict(ISSUE, errorReportCount="1"), ISSUE])
    section = crash_digest.google_section(client, cfg.app("lumen-android"), crash_digest.last_full_week(TODAY), 3)
    assert section.lines[0] == "3 crashes, 1 freeze (week before: 4 crashes)."
    assert section.lines[1] == "By version: code 28: 3 crashes · code 27: 1 freeze"
    assert section.lines[3].startswith("• Crash: java.lang.IndexOutOfBoundsException in com.x.Player.seek · "
                                       "3 reports, 2 people, code 28\n  <https://play.google.com/")
    # The issue window is the same Pacific week, in UTC: 8:00 AM (standard time).
    assert client.issue_params["interval.startTime.hours"] == 8
    assert client.issue_params["interval.startTime.day"] == 16
    assert client.issue_params["interval.endTime.day"] == 23


def test_google_stops_where_its_numbers_end(cfg):
    client = FakePlay([count_row("2026-11-16", "ANR", "28", 1)], [], fresh=(2026, 11, 22))
    section = crash_digest.google_section(client, cfg.app("lumen-android"), crash_digest.last_full_week(TODAY), 3)
    assert section.lines[0] == "1 freeze (Google's numbers run to Nov 21) (week before: none)."

    empty = FakePlay([], [], fresh=(2026, 11, 22))
    section = crash_digest.google_section(empty, cfg.app("lumen-android"), crash_digest.last_full_week(TODAY), 3)
    assert section.lines == ["No crashes or freezes reported (Google's numbers run to Nov 21) (week before: none)."]


def test_run_posts_once_per_app_and_week(conn, digest_cfg, capsys):
    discord = FakeDiscord()
    play_client = FakePlay([count_row("2026-11-17", "CRASH", "28", 1)], [ISSUE])
    kwargs = {"today": TODAY, "asc_client": FakeAsc({}), "play_client": play_client,
              "transport": httpx.MockTransport(discord)}
    assert crash_digest.run(digest_cfg, conn, **kwargs)
    [(url, text)] = discord.posts
    assert url == LUMEN_HOOK
    assert text.startswith("**Lumen: crashes for Nov 16–22**\n**iOS (Apple):** No crashes reported")
    assert "**Android (Google Play):** 1 crash (week before: none)." in text

    assert crash_digest.run(digest_cfg, conn, **kwargs)  # same week again: nothing new
    assert len(discord.posts) == 1
    assert crash_digest.run(digest_cfg, conn, again=True, **kwargs)
    assert len(discord.posts) == 2
    assert "secret" not in capsys.readouterr().out
    assert db.last_runs(conn)["crash_digest"]["last"]["ok"] == 1


def test_failures_go_to_admin_alerts(conn, digest_cfg, capsys):
    discord = FakeDiscord()
    cfg = dataclasses.replace(digest_cfg, crash_digest={"apps": ["Lumen", "Tandem", "Nope"]})
    ok = crash_digest.run(cfg, conn, today=TODAY, asc_client=FakeAsc({}, fail=True),
                          play_client=FakePlay([], []), transport=httpx.MockTransport(discord))
    assert not ok
    (lumen_url, lumen), (admin_url, admin) = discord.posts
    assert lumen_url == LUMEN_HOOK
    assert "Couldn't read Apple's crash report this week: App Store Connect had a server error" in lumen
    assert admin_url == ADMIN_HOOK
    assert admin.startswith("**ASO Hub crash digest for Nov 16–22: something failed**")
    assert "Tandem: no #alerts webhook saved (alerts-tandem)" in admin
    assert "Nope is listed under [crash_digest]" in admin
    assert "Lumen, iOS (Apple): App Store Connect had a server error" in admin
    assert admin.endswith("Posted: Lumen.")
    assert "secret" not in capsys.readouterr().err
    assert db.last_runs(conn)["crash_digest"]["last"]["ok"] == 0


def test_an_app_without_apple_reports_is_a_note_not_a_failure(conn, digest_cfg):
    class NoRequest(FakeAsc):
        def paged(self, path, params=None):
            if path.endswith("/analyticsReportRequests"):
                return iter(())
            return super().paged(path, params)

    discord = FakeDiscord()
    assert crash_digest.run(digest_cfg, conn, today=TODAY, asc_client=NoRequest({}),
                            play_client=FakePlay([], []), transport=httpx.MockTransport(discord))
    [(_, text)] = discord.posts
    assert "**iOS (Apple):** No Apple crash numbers yet" in text


def test_a_failed_post_is_not_marked_sent(conn, digest_cfg):
    discord = FakeDiscord(status=500)
    kwargs = {"today": TODAY, "asc_client": FakeAsc({}), "play_client": FakePlay([], []),
              "transport": httpx.MockTransport(discord)}
    assert not crash_digest.run(digest_cfg, conn, **kwargs)
    assert conn.execute("SELECT COUNT(*) FROM crash_digests").fetchone()[0] == 0


def test_dry_run_posts_nothing(conn, digest_cfg, capsys):
    discord = FakeDiscord()
    assert crash_digest.run(digest_cfg, conn, today=TODAY, dry_run=True, asc_client=FakeAsc({}),
                            play_client=FakePlay([], []), transport=httpx.MockTransport(discord))
    assert discord.posts == []
    assert "**Lumen: crashes for Nov 16–22**" in capsys.readouterr().out


def test_long_messages_drop_issues_to_fit():
    issues = [crash_digest._issue_line(dict(ISSUE, location="x" * 80)) for _ in range(40)]
    week = crash_digest.last_full_week(TODAY)
    text = crash_digest.message("Lumen", week, [crash_digest.Section("Android (Google Play)",
                                                                      ["1 crash.", "Top issues:", *issues])])
    assert len(text) <= crash_digest.LIMIT
    assert text.count("• Crash") < 40 and text.rstrip().endswith("details>")


def test_webhook_files_are_named_after_the_app(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, alerts_webhook_dir=tmp_path)
    assert cfg.alerts_webhook_file("Atlas").name == "alerts-atlas"
    assert cfg.alerts_webhook_file("Pocket Notes").name == "alerts-pocket-notes"



def test_an_unexpected_error_still_reaches_admin_alerts(conn, digest_cfg, monkeypatch):
    discord = FakeDiscord()

    def boom(*args, **kwargs):
        raise KeyError("secret-link")

    monkeypatch.setattr(crash_digest, "_apple", boom)
    ok = crash_digest.run(digest_cfg, conn, today=TODAY, asc_client=FakeAsc({}), play_client=FakePlay([], []),
                          transport=httpx.MockTransport(discord))
    assert not ok
    [(url, text)] = discord.posts
    assert url == ADMIN_HOOK
    assert "Lumen: unexpected error (KeyError)" in text and "secret-link" not in text
    assert db.last_runs(conn)["crash_digest"]["last"]["ok"] == 0
