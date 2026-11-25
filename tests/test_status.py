import dataclasses

from fastapi.testclient import TestClient

from aso_hub import status, web

SECRET = "do-not-show-this"  # noqa: S105 (a marker to search the pages for)
PAGES = ("/settings", "/settings/connections", "/settings/apps", "/settings/alerts", "/settings/schedule",
         "/settings/sign-in")


def test_the_settings_pages_show_the_setup_but_never_a_secret(cfg, tmp_path, monkeypatch):
    for name in ("asc-key.p8", "asobeast-token", "discord-webhook", "alerts-lumen"):
        (tmp_path / name).write_text(f"{SECRET}-{name}\n")
    for var, name in (("ASC_KEY_PATH", "asc-key.p8"), ("ASOBEAST_TOKEN_FILE", "asobeast-token"),
                      ("DISCORD_WEBHOOK_FILE", "discord-webhook")):
        monkeypatch.setenv(var, str(tmp_path / name))
    monkeypatch.setenv("ALERTS_WEBHOOK_DIR", str(tmp_path))
    monkeypatch.setenv("ASC_ISSUER_ID", f"{SECRET}-issuer")
    monkeypatch.setenv("ASC_VENDOR_NUMBER", f"{SECRET}-vendor")
    page = TestClient(web.app)
    texts = {path: page.get(path) for path in PAGES}
    assert all(r.status_code == 200 for r in texts.values()), {p: r.status_code for p, r in texts.items()}
    assert not any(SECRET in r.text for r in texts.values())
    assert "From a file on the server" in texts["/settings/connections"].text
    assert "Lumen (Apple)" in texts["/settings/apps"].text
    assert "A keyword drops" in texts["/settings/alerts"].text
    assert "Crash digest: Tandem" in texts["/settings/alerts"].text


def test_every_rule_and_threshold_has_a_plain_name(cfg):
    for key, rule in cfg.rules.items():
        assert key in status.RULES
        assert set(rule) - {"enabled"} <= set(status.THRESHOLDS)


def test_a_missing_or_unreadable_secret_is_said_not_raised(cfg, tmp_path):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    settings = dataclasses.replace(cfg, asc_key_path=folder, google_key_path=tmp_path / "nope")
    states = status.secret_states(settings)
    assert states["asc-key.p8"]["state"] == "A file the hub can't read"
    assert states["google-play.json"] == {"ok": False, "state": "Not saved", "saved": False}


def test_every_page_shows_the_version(cfg):
    for path in ("/", "/actions", "/settings"):
        text = TestClient(web.app).get(path).text
        assert f"Version {status.VERSION}" in text and "github.com/LiftbridgeLabs/aso-hub/issues" in text
