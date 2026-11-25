import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from aso_hub import auth, db, oidc, web

ISSUER = "https://id.example.com"
PASSWORD = "correct horse battery"  # noqa: S105 (a test password)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pytestmark = pytest.mark.real_sign_in


class Provider:
    def __init__(self):
        self.claims = {"email": "me@example.com", "email_verified": True}
        self.nonce = ""
        self.sign_with = KEY
        self.token_requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/keys"})
        if path == "/token":
            self.token_requests.append(request)
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "hub", "sub": "1", "iat": now, "exp": now + 300,
                      "nonce": self.nonce} | self.claims
            token = jwt.encode(claims, self.sign_with, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token})
        if path == "/keys":
            public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
            return httpx.Response(200, json={"keys": [public | {"kid": "k1", "use": "sig"}]})
        return httpx.Response(404)


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    real = oidc.client
    monkeypatch.setattr(oidc, "client", lambda: real(httpx.MockTransport(fake)))
    return fake


@pytest.fixture
def owner(cfg, provider, monkeypatch):
    monkeypatch.setattr(auth, "FAILURES", {})
    hub = TestClient(web.app, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)
    conn = db.connect(cfg.database_path)
    hub.post("/setup", data={"code": auth.setup_code(conn), "password": PASSWORD, "again": PASSWORD})
    r = hub.post("/settings/sign-in/provider", data={
        "current": PASSWORD, "name": "Home ID", "issuer": ISSUER, "client_id": "hub",
        "client_secret": "s3cret-value", "enabled": "on"})
    assert r.status_code == 303, r.text
    hub.post("/settings/sign-in/allowed", data={"current": PASSWORD, "allowed": "me@example.com"})
    assert "s3cret-value" not in hub.get("/settings/sign-in").text
    assert "s3cret-value" not in conn.execute("SELECT client_secret FROM sign_in_providers").fetchone()[0]
    conn.close()
    hub.cookies.clear()
    return hub


def go(hub, provider, query=""):
    """Click the sign-in button and come back from the provider."""
    start = hub.get(f"/auth/oidc/1{query}")
    assert start.status_code == 303, start.text
    sent = parse_qs(urlsplit(start.headers["location"]).query)
    provider.nonce = sent["nonce"][0]
    assert sent["code_challenge_method"] == ["S256"] and sent["redirect_uri"] == ["http://testserver/auth/oidc/callback"]
    return sent["state"][0]


def back(hub, state):
    return hub.get("/auth/oidc/callback", params={"state": state, "code": "abc"})


def test_single_sign_on_signs_in_an_allowed_address(owner, provider):
    assert "Sign in with Home ID" in owner.get("/login").text
    r = back(owner, go(owner, provider))
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert owner.get("/").status_code == 200
    sent = parse_qs(provider.token_requests[0].content.decode())
    assert sent["code_verifier"] and provider.token_requests[0].headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize("change", [
    {"email": "stranger@example.com"}, {"email_verified": False}, {"aud": "another-app"},
    {"iss": "https://evil.example"}, {"exp": 1}, {"nonce": "someone else's"}, {"email": None},
    {"aud": ["hub", "other"]}])
def test_a_wrong_answer_signs_nobody_in(owner, provider, change):
    state = go(owner, provider)
    provider.claims |= change
    assert back(owner, state).status_code == 401
    assert owner.get("/").status_code == 303


def test_an_answer_signed_by_someone_else_is_refused(owner, provider):
    state = go(owner, provider)
    provider.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert back(owner, state).status_code == 401


def test_a_sign_in_works_once_and_only_in_the_browser_that_started_it(owner, provider):
    state = go(owner, provider)
    other = TestClient(web.app, follow_redirects=False)
    assert back(other, state).status_code == 401
    assert back(owner, state).status_code == 303
    owner.cookies.clear()
    owner.cookies.set("aso_hub_oidc", state)
    assert back(owner, state).status_code == 401


def test_try_it_signs_nobody_in_or_out(owner, provider):
    owner.post("/login", data={"password": PASSWORD})
    before = owner.cookies.get("aso_hub_session")
    r = back(owner, go(owner, provider, "?test=true"))
    assert "It worked" in r.text and "may sign in" in r.text
    assert owner.cookies.get("aso_hub_session") == before


def test_a_stranger_cant_use_try_it_to_get_around_a_turned_off_provider(owner, cfg):
    conn = db.connect(cfg.database_path)
    with conn:
        conn.execute("UPDATE sign_in_providers SET enabled = 0")
    conn.close()
    assert owner.get("/auth/oidc/1?test=true").status_code == 400


def test_a_new_issuer_needs_the_secret_again(owner):
    owner.post("/login", data={"password": PASSWORD})
    r = owner.post("/settings/sign-in/provider", data={
        "current": PASSWORD, "id": "1", "name": "x", "issuer": "https://evil.example", "client_id": "hub"})
    assert r.status_code == 400 and "Enter the client secret" in r.text


@pytest.mark.parametrize("change", ["provider", "allowed", "provider-remove", "two-step-off", "password"])
def test_changes_to_who_gets_in_ask_for_the_password(owner, change):
    owner.post("/login", data={"password": PASSWORD})
    assert owner.post(f"/settings/sign-in/{change}", data={"current": "not it, not it"}).status_code == 400


@pytest.mark.parametrize("address", ["http://id.example.com", "ftp://x", "id.example.com", "https://x.example/?a=1"])
def test_an_issuer_must_be_https(address):
    with pytest.raises(oidc.OidcError):
        oidc.check_issuer_address(address)


def test_the_allowed_list():
    allowed = oidc.parse_allowed("Me@Example.com, @team.example\n")
    assert allowed == ["me@example.com", "@team.example"]
    assert oidc.is_allowed("a@team.example", allowed) and not oidc.is_allowed("a@evil-team.example", allowed)
    assert not oidc.is_allowed("team.example", allowed)
    with pytest.raises(oidc.OidcError):
        oidc.parse_allowed("everyone")
