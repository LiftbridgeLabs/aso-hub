import pytest
from fastapi.testclient import TestClient

from aso_hub import notify, web
from aso_hub.access import AccessCheck


@pytest.mark.parametrize("host, status", [("127.0.0.1:8690", 200), ("localhost", 200), ("[::1]:8690", 200),
                                          ("hub.example.com", 200), ("attacker.example", 400), ("", 400)])
def test_only_known_addresses_are_answered(cfg, monkeypatch, host, status):
    monkeypatch.setenv("ASO_HUB_URL", "https://hub.example.com")
    assert TestClient(web.app).get("/", headers={"host": host}).status_code == status


def test_extra_addresses_can_be_allowed(cfg, monkeypatch):
    page = TestClient(web.app)
    monkeypatch.setenv("ASO_HUB_ALLOWED_HOSTS", "192.168.1.20")
    assert page.get("/", headers={"host": "192.168.1.20:8690"}).status_code == 200
    monkeypatch.setenv("ASO_HUB_ALLOWED_HOSTS", "*")
    assert page.get("/", headers={"host": "anything.example"}).status_code == 200


def test_pages_carry_the_browser_protections(cfg):
    headers = TestClient(web.app).get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"


def test_a_half_set_up_cloudflare_sign_in_refuses_to_run():
    with pytest.raises(RuntimeError, match="CF_ACCESS_TEAM_DOMAIN"):
        AccessCheck(None, audience="aud", team_domain="")


def test_strangers_text_cant_pose_as_a_discord_link():
    sent = []

    class Client:
        def post(self, url, params, json):
            sent.append(json["content"])
            return type("R", (), {"status_code": 200})()

    notify._post(Client(), "https://discord.com/api/webhooks/1/x", "Click [your bank](https://evil.example)")
    assert sent == ["Click [your bank]\uff08https://evil.example)"]


@pytest.fixture
def locked(cfg, tmp_path, monkeypatch):
    """A hub with a password, opened as its tests call it."""
    secret = tmp_path / "hub-password"
    secret.write_text("correct horse battery\n")
    monkeypatch.setenv("HUB_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("ASO_HUB_BEHIND_SIGN_IN")
    monkeypatch.setattr("aso_hub.guard.FAILURES", {})
    return TestClient(web.app)


def test_the_password_is_asked_for_and_accepted(locked):
    first = locked.get("/")
    assert first.status_code == 401 and first.headers["www-authenticate"].startswith("Basic")
    assert first.headers["x-frame-options"] == "DENY"
    assert locked.get("/", auth=("anyone", "wrong password")).status_code == 401
    assert locked.get("/", auth=("anyone", "correct horse battery")).status_code == 200
    assert locked.get("/static/hub.css").status_code == 401
    assert locked.get("/healthz").status_code == 200


@pytest.mark.parametrize("header", ["Basic !!!", "Basic", "Bearer x", "Basic /w=="])
def test_a_garbled_sign_in_is_refused(locked, header):
    assert locked.get("/", headers={"authorization": header}).status_code == 401


def test_repeated_wrong_passwords_are_locked_out(locked, monkeypatch):
    from aso_hub import guard

    clock = [1000.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])
    for _ in range(15):  # visits without a password don't count as guesses
        assert locked.get("/").status_code == 401
    for _ in range(guard.MAX_FAILURES):
        assert locked.get("/", auth=("a", "nope")).status_code == 401
    assert locked.get("/", auth=("a", "correct horse battery")).status_code == 429
    clock[0] += guard.WINDOW + 1
    assert locked.get("/", auth=("a", "correct horse battery")).status_code == 200


def test_a_short_password_is_refused(locked, tmp_path):
    (tmp_path / "hub-password").write_text("short")
    assert locked.get("/", auth=("a", "short")).status_code == 503


@pytest.mark.parametrize("host", ["testserver", "localhost"])
def test_opened_wider_without_a_sign_in_it_shows_nothing(cfg, monkeypatch, host):
    monkeypatch.delenv("ASO_HUB_BEHIND_SIGN_IN")
    answer = TestClient(web.app).get("/", headers={"host": host})
    assert answer.status_code == 503 and "needs a sign-in" in answer.text


def test_a_local_hub_needs_no_password(cfg, monkeypatch):
    monkeypatch.delenv("ASO_HUB_BEHIND_SIGN_IN")
    monkeypatch.delenv("ASO_HUB_ALLOWED_HOSTS")
    monkeypatch.delenv("ASO_HUB_URL", raising=False)
    assert TestClient(web.app).get("/", headers={"host": "127.0.0.1:8690"}).status_code == 200


def test_cloudflare_access_counts_as_a_sign_in(monkeypatch):
    from aso_hub import guard

    monkeypatch.delenv("ASO_HUB_BEHIND_SIGN_IN")
    assert not guard.signs_in_elsewhere()
    monkeypatch.setenv("CF_ACCESS_AUD", "aud")
    assert guard.signs_in_elsewhere()
