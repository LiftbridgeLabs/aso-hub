import dataclasses
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from aso_hub import db, overlay, scheduler, web, web_settings

HOOK = "https://discord.com/api/webhooks/1/very-secret"
P8 = "-----BEGIN PRIVATE KEY-----\nsecretsecret\n-----END PRIVATE KEY-----"


@pytest.fixture
def hub(cfg):
    return TestClient(web.app, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)


def now_settings(cfg):
    conn = db.connect(cfg.database_path)
    try:
        return overlay.effective(cfg, conn)
    finally:
        conn.close()


def post(hub, path, **data):
    r = hub.post(path, data=data)
    assert r.status_code == 303, r.text
    return r


def test_keys_saved_in_the_app_are_encrypted_used_and_never_shown(cfg, hub):
    post(hub, "/settings/connections/save", which="asc", asc_issuer_id="issuer-1", asc_key_id="KEY1",
         asc_vendor_number="123", key=P8)
    now = now_settings(cfg)
    assert now.asc_issuer_id == "issuer-1" and now.read_secret(now.asc_key_path) == P8
    assert "secretsecret" not in hub.get("/settings/connections").text
    conn = db.connect(cfg.database_path)
    assert "secretsecret" not in conn.execute("SELECT value FROM app_secrets").fetchone()[0]
    conn.close()
    assert (cfg.database_path.parent / "secret.key").stat().st_mode & 0o077 == 0
    post(hub, "/settings/connections/remove", name="asc-key.p8")
    assert now_settings(cfg).asc_key_path == cfg.asc_key_path


def test_another_encryption_key_asks_for_the_secret_again(cfg, hub, monkeypatch):
    post(hub, "/settings/alerts/webhook", name="discord-webhook", url=HOOK)
    monkeypatch.setenv("SECRET_KEY", "a different key")
    now = now_settings(cfg)
    assert "discord-webhook" in now.broken_secrets
    assert "enter it again" in hub.get("/settings/alerts").text


def test_a_file_standing_in_for_a_broken_secret_is_said(cfg, hub, tmp_path, monkeypatch):
    post(hub, "/settings/alerts/webhook", name="discord-webhook", url=HOOK)
    (tmp_path / "hook").write_text(HOOK)
    monkeypatch.setenv("DISCORD_WEBHOOK_FILE", str(tmp_path / "hook"))
    monkeypatch.setenv("SECRET_KEY", "a different key")
    assert "Using the file on the server" in hub.get("/settings/alerts").text


@pytest.mark.parametrize("data", [
    {"which": "asc", "key": "not a key"}, {"which": "play", "key": "{}"}, {"which": "play", "key": "nonsense"},
    {"which": "play", "play_bucket": "a bucket!"}, {"which": "asobeast", "asobeast_url": "file:///etc"}])
def test_wrong_things_are_refused(cfg, hub, data):
    assert hub.post("/settings/connections/save", data=data).status_code == 400


@pytest.mark.parametrize("url", ["https://evil.example/hook", "http://discord.com/api/webhooks/1/x"])
def test_only_discord_webhooks_are_saved(cfg, hub, url):
    assert hub.post("/settings/alerts/webhook", data={"name": "discord-webhook", "url": url}).status_code == 400
    assert hub.post("/settings/alerts/webhook", data={"name": "../x", "url": HOOK}).status_code == 400


def test_a_saved_asobeast_token_cant_be_sent_somewhere_new(cfg, hub, tmp_path, monkeypatch):
    (tmp_path / "token").write_text("from-a-file")
    monkeypatch.setenv("ASOBEAST_TOKEN_FILE", str(tmp_path / "token"))
    r = hub.post("/settings/connections/save", data={"which": "asobeast", "asobeast_url": "http://evil.example"})
    assert r.status_code == 400 and "Enter the token again" in r.text
    post(hub, "/settings/connections/save", which="asobeast", asobeast_url="http://other.example",
         token="typed")  # noqa: S106 (a test token)
    post(hub, "/settings/connections/remove", name="asobeast-token")
    now = now_settings(cfg_with_token(cfg, tmp_path))
    assert now.asobeast_url == cfg.asobeast_url  # the file's token goes only where the files say


def cfg_with_token(cfg, tmp_path):
    return dataclasses.replace(cfg, asobeast_token_file=tmp_path / "token")


def test_apps_can_be_added_renamed_hidden_and_shown(cfg, hub):
    post(hub, "/settings/apps/add", name="Pocket Notes", store="app_store", store_id="987654321", markets="us, GB")
    app = now_settings(cfg).app("pocket-notes-ios")
    assert (app.apple_id, app.markets, app.saved) == ("987654321", ["us", "gb"], True)
    post(hub, "/settings/apps/edit", slug="lumen-ios", name="Lumen Pro", markets="us")
    assert now_settings(cfg).app("lumen-ios").name == "Lumen Pro"
    assert [a.slug for a in now_settings(cfg).apps].count("lumen-ios") == 1
    post(hub, "/settings/apps/reset", slug="lumen-ios")
    assert now_settings(cfg).app("lumen-ios").name == "Lumen"
    post(hub, "/settings/apps/hide", slug="lumen-ios")
    assert now_settings(cfg).app("lumen-ios") is None
    post(hub, "/settings/apps/show", store_id="1000000001")
    assert now_settings(cfg).app("lumen-ios")
    assert hub.get("/apps/pocket-notes-ios").status_code == 200


@pytest.mark.parametrize("data", [
    {"name": "x", "store": "app_store", "store_id": "abc"}, {"name": "", "store": "app_store", "store_id": "987654321"},
    {"name": "x", "store": "google_play", "store_id": "../../x"},
    {"name": "x", "store": "app_store", "store_id": "987654321", "markets": "usa"},
    {"name": "x", "store": "app_store", "store_id": "1000000001"}])
def test_a_bad_app_is_refused(cfg, hub, data):
    assert hub.post("/settings/apps/add", data=data).status_code == 400


def test_rules_and_the_digest(cfg, hub):
    data = {"keyword_drop.enabled": "on", "keyword_drop.places": "20", "keyword_drop.days": "4",
            "keyword_drop.from_top": "30", "low_downloads.enabled": "on", "low_downloads.days": "7",
            "low_downloads.min_per_day.kettle-ios": "2.5"}
    post(hub, "/settings/alerts/rules", **data)
    rules = now_settings(cfg).rules
    assert rules["keyword_drop"] == {"enabled": True, "places": 20, "days": 4, "from_top": 30}
    assert rules["low_downloads"]["min_per_day"] == {"kettle-ios": 2.5}
    assert rules["new_review"]["enabled"] is False
    assert hub.post("/settings/alerts/rules", data={"keyword_drop.days": "zero"}).status_code == 400
    assert hub.post("/settings/alerts/rules", data={"keyword_drop.days": "0"}).status_code == 400
    post(hub, "/settings/alerts/digest", apps="Lumen", top_issues="5")
    post(hub, "/settings/alerts/webhook", name="alerts-lumen", url=HOOK)
    now = now_settings(cfg)
    assert now.crash_digest == {"apps": ["Lumen"], "top_issues": 5}
    assert now.read_secret(now.alerts_webhook_file("Lumen")) == HOOK
    assert "very-secret" not in hub.get("/settings/alerts").text


def test_a_webhook_test_sends_a_message(cfg, hub):
    post(hub, "/settings/alerts/webhook", name="discord-webhook", url=HOOK)
    sent = []

    def discord(request):
        sent.append(request)
        return httpx.Response(200, json={})

    message = web_settings.test_connection(now_settings(cfg), "discord-webhook", httpx.MockTransport(discord))
    assert "sent" in message and len(sent) == 1
    with pytest.raises(web_settings.Refused, match="Save a Discord webhook"):
        web_settings.test_connection(cfg, "discord-webhook")


def taken(conn, now):
    return list(scheduler.due(conn, now))


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def test_the_schedule(cfg, conn):
    assert taken(conn, at("2026-11-25 09:00")) == ["pull"]  # a new hub pulls straight away
    assert taken(conn, at("2026-11-25 09:01")) == []
    assert taken(conn, at("2026-11-25 13:00")) == ["pull"]
    assert taken(conn, at("2026-11-29 12:00")) == ["pull"]  # missed times run once
    assert taken(conn, at("2026-11-30 08:00")) == ["pull", "digest"]  # Monday
    assert taken(conn, at("2026-11-30 08:00")) == []


def test_the_schedule_follows_its_time_zone(cfg, conn):
    db.set_setting(conn, "schedule", {"timezone": "America/Denver", "pull_times": ["07:00"]})
    taken(conn, at("2026-11-25 20:00"))
    assert taken(conn, at("2026-11-26 13:59")) == []
    assert taken(conn, at("2026-11-26 14:00")) == ["pull"]


def test_saving_a_schedule_doesnt_run_a_time_already_past(cfg, hub, conn):
    taken(conn, dt.datetime.now(dt.UTC))
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)).strftime("%H:%M")
    post(hub, "/settings/schedule/save", enabled="on", timezone="UTC", pull_times=past, digest_day="0",
         digest_time="08:00")
    assert taken(conn, dt.datetime.now(dt.UTC)) == []


@pytest.mark.parametrize("data", [{"timezone": "Mars/Base"}, {"pull_times": "25:00"}, {"pull_times": ""},
                                  {"digest_day": "9"}, {"digest_time": "late"}])
def test_a_bad_schedule_is_refused(cfg, hub, data):
    good = {"timezone": "UTC", "pull_times": "07:00", "digest_day": "0", "digest_time": "08:00"}
    assert hub.post("/settings/schedule/save", data=good | data).status_code == 400


def test_one_job_runs_at_a_time(cfg, hub):
    with scheduler.RUNNING:
        r = hub.post("/settings/schedule/pull")
    assert r.status_code == 400 and "already running" in r.text


def test_turned_off_nothing_is_due(cfg, conn):
    db.set_setting(conn, "schedule", {"enabled": False})
    assert taken(conn, at("2026-11-30 09:00")) == []


def test_a_job_not_reached_before_a_restart_is_still_due(cfg, conn):
    taken(conn, at("2026-11-25 09:00"))
    started = next(scheduler.due(conn, at("2026-11-30 08:00")))  # the hub stops during this one
    assert started == "pull" and taken(conn, at("2026-11-30 08:01")) == ["digest"]


def test_a_hidden_apps_address_isnt_reused(cfg, hub):
    post(hub, "/settings/apps/add", name="Twin", store="app_store", store_id="111111111")
    post(hub, "/settings/apps/hide", slug="twin-ios")
    post(hub, "/settings/apps/add", name="Twin", store="app_store", store_id="222222222")
    post(hub, "/settings/apps/show", store_id="111111111")
    assert {a.slug for a in now_settings(cfg).apps} >= {"twin-ios", "twin-2-ios"}
