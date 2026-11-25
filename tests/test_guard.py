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
