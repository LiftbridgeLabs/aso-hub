import dataclasses
import datetime as dt
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from aso_hub import db, play, rules, web

TODAY = dt.date(2026, 10, 28)
PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
KEY = {"client_email": "aso-hub@example.iam.gserviceaccount.com", "private_key": PEM}


def issue(iid, kind="CRASH", last="2026-10-27T13:00:00Z", users="1", reports="1"):
    return {"name": f"apps/com.example.lumen/{iid}", "type": kind, "cause": "Unknown Source - ad.f.a",
            "location": "java.net.UnknownHostException", "errorReportCount": reports, "distinctUsers": users,
            "lastErrorReportTime": last, "lastAppVersion": {"versionCode": "21"},
            "issueUri": f"https://play.google.com/console/developers/1/app/2/vitals/crashes/{iid}/details"}


class FakeGoogle:
    def __init__(self, token_status=200, issues_status=200):
        self.calls = []
        self.token_status, self.issues_status = token_status, issues_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(self.token_status, json={"access_token": "t", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer t"
        if self.issues_status != 200:
            return httpx.Response(self.issues_status, json={})
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"errorIssues": [issue("i2", kind="APPLICATION_NOT_RESPONDING")]})
        return httpx.Response(200, json={"errorIssues": [issue("i1")], "nextPageToken": "p2"})


def client(fake):
    return play.Client(KEY, transport=httpx.MockTransport(fake))


def only(cfg, *names):
    return dataclasses.replace(cfg, rules={n: r | {"enabled": n in names} for n, r in cfg.rules.items()})


def test_pull_reads_every_page_and_saves_each_android_app(cfg, conn):
    fake = FakeGoogle()
    # The fake answers the same two issues for each Android app (Lumen and Beacon).
    assert play.pull(conn, cfg, client(fake), TODAY) == "read 4 crash and ANR issues"
    for slug in ("lumen-android", "beacon-android"):
        body, _ = db.latest_snapshot(conn, "play_issues", slug)
        assert [i["type"] for i in body["issues"]] == ["CRASH", "APPLICATION_NOT_RESPONDING"]
    searches = [r for r in fake.calls if r.url.host != "oauth2.googleapis.com"]
    for r in searches:  # every page asks for the same 30 days, crashes and ANRs only
        assert r.url.params["filter"] == "errorIssueType = CRASH OR errorIssueType = ANR"
        assert r.url.params["interval.endTime.timeZone.id"] == "UTC"
        assert r.url.params["interval.startTime.day"] and r.url.params["interval.endTime.hours"]
    paths = {r.url.path for r in searches}
    assert paths == {"/v1beta1/apps/com.example.lumen/errorIssues:search",
                     "/v1beta1/apps/com.example.beacon/errorIssues:search"}
    assert sum(r.url.host == "oauth2.googleapis.com" for r in fake.calls) == 1  # one token, reused


@pytest.mark.parametrize("fake, words", [(FakeGoogle(token_status=400), "refused the hub's service account key"),
                                         (FakeGoogle(issues_status=403), "still a user in Play Console")])
def test_refusals_give_a_plain_message_and_save_nothing(cfg, conn, fake, words):
    with pytest.raises(play.PlayError, match=words):
        play.pull(conn, cfg, client(fake), TODAY)
    assert db.latest_snapshot(conn, "play_issues", "lumen-android") == (None, None)


def test_no_key_says_so(cfg, conn):
    with pytest.raises(play.PlayError, match="isn't set up"):
        play.pull(conn, cfg)


def save_issues(conn, issues):
    with conn:
        db.save_snapshot(conn, "2026-10-28", "play_issues", "lumen-android", {"issues": issues})


def test_recent_crashes_are_todos_with_a_link(cfg, conn, monkeypatch):
    monkeypatch.setattr(rules, "_today", lambda: TODAY)
    save_issues(conn, [issue("i1"), issue("i2", kind="APPLICATION_NOT_RESPONDING", users="4", reports="6"),
                       issue("old", last="2026-10-10T13:00:00Z")])
    found = rules.findings(only(cfg, "play_crash"), conn)
    assert [(f.key, f.title, f.priority) for f in found] == [
        ("i1", "Crash on Android", "medium"), ("i2", "ANR (app froze) on Android", "high")]
    assert found[1].detail == ("Lumen (Android): java.net.UnknownHostException in Unknown Source - ad.f.a. "
                               "6 reports from 4 users in 30 days, version code 21, last on Oct 27.")
    assert found[0].link.endswith("/crashes/i1/details") and found[0].link_text == "Open it in Play Console"

    rules.evaluate(only(cfg, "play_crash"), conn)
    page = TestClient(web.app).get("/actions").text
    assert 'href="https://play.google.com/console/developers/1/app/2/vitals/crashes/i1/details"' in page


def test_android_page_lists_crashes(cfg):
    conn = db.connect(cfg.database_path)
    save_issues(conn, [issue("i1")])
    conn.close()
    text = TestClient(web.app).get("/apps/lumen-android").text
    assert "Crashes and ANRs" in text and "java.net.UnknownHostException" in text


def test_older_databases_get_the_link_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE hub_actions (rule TEXT, app_slug TEXT, key TEXT, title TEXT, detail TEXT, "
                "priority TEXT, kind TEXT, first_seen TEXT, last_seen TEXT, resolved_at TEXT, "
                "PRIMARY KEY (rule, app_slug, key))")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert {"link", "link_text"} <= {r["name"] for r in conn.execute("PRAGMA table_info(hub_actions)")}
    conn.close()


def test_key_file_is_read_from_settings(cfg, conn, tmp_path, monkeypatch):
    path = tmp_path / "google-play.json"
    path.write_text(json.dumps(KEY))
    real, seen = play.Client, []

    def fake_client(key):
        seen.append(key["client_email"])
        return real(key, transport=httpx.MockTransport(FakeGoogle()))

    monkeypatch.setattr(play, "Client", fake_client)
    assert play.pull(conn, dataclasses.replace(cfg, google_key_path=path), today=TODAY).startswith("read 4")
    assert seen == [KEY["client_email"]]


def test_interval_is_the_last_30_days_on_whole_hours():
    p = play._interval(dt.datetime(2026, 10, 29, 3, 41, 12, tzinfo=dt.UTC), 30)
    assert (p["interval.startTime.month"], p["interval.startTime.day"], p["interval.startTime.hours"]) == (9, 29, 3)
    assert (p["interval.endTime.month"], p["interval.endTime.day"], p["interval.endTime.hours"]) == (10, 29, 3)
