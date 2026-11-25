import dataclasses

from fastapi.testclient import TestClient

from aso_hub import status, web

SECRET = "do-not-show-this"  # noqa: S105 (a marker to search the page for)


def test_the_settings_page_shows_the_setup_but_never_a_secret(cfg, tmp_path, monkeypatch):
    for name in ("asc-key.p8", "asobeast-token", "discord-webhook", "alerts-lumen", "hub-password"):
        (tmp_path / name).write_text(f"{SECRET}-{name}\n")
    for var, name in (("ASC_KEY_PATH", "asc-key.p8"), ("ASOBEAST_TOKEN_FILE", "asobeast-token"),
                      ("DISCORD_WEBHOOK_FILE", "discord-webhook")):
        monkeypatch.setenv(var, str(tmp_path / name))
    monkeypatch.setenv("ALERTS_WEBHOOK_DIR", str(tmp_path))
    monkeypatch.setenv("HUB_PASSWORD_FILE", str(tmp_path / "hub-password"))
    monkeypatch.setenv("ASC_ISSUER_ID", f"{SECRET}-issuer")
    monkeypatch.setenv("ASC_VENDOR_NUMBER", f"{SECRET}-vendor")
    answer = TestClient(web.app).get("/settings", auth=("me", f"{SECRET}-hub-password"))
    assert answer.status_code == 200
    text = answer.text
    assert SECRET not in text
    assert "The hub&#39;s password." in text and "Another sign-in service" in text
    assert "Run set-secret.sh alerts-tandem" in text and "Put google-play.json in secrets/" in text
    assert "Lumen (Apple)" in text and "A keyword drops" in text and "places lost: 15" in text


def test_it_needs_the_same_sign_in_as_the_other_pages(cfg, tmp_path, monkeypatch):
    (tmp_path / "pw").write_text("correct horse battery")
    monkeypatch.setenv("HUB_PASSWORD_FILE", str(tmp_path / "pw"))
    monkeypatch.setattr("aso_hub.guard.FAILURES", {})
    assert TestClient(web.app).get("/settings").status_code == 401


def test_every_rule_and_threshold_has_a_plain_name(cfg):
    for key, rule in cfg.rules.items():
        assert key in status.RULES
        assert set(rule) - {"enabled"} <= set(status.THRESHOLDS)


def test_a_missing_or_unreadable_secret_is_said_not_raised(cfg, tmp_path):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    settings = dataclasses.replace(cfg, asc_key_path=folder, google_key_path=tmp_path / "nope")
    states = {i["name"]: i["state"] for g in status.sources(settings) for i in g["items"]}
    assert states["Key file"] == "Saved, but the hub can't read it"
    assert states["Service account key"] == "Not saved"


def test_every_page_shows_the_version(cfg):
    for path in ("/", "/actions", "/settings"):
        assert f"ASO Hub {status.VERSION}" in TestClient(web.app).get(path).text
