import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aso_hub.access import TEAM_DOMAIN, AccessCheck

AUD = "test-audience"


class FakeKeys:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return self


@pytest.fixture
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def client(keys):
    app = FastAPI()
    app.add_middleware(AccessCheck, audience=AUD, jwks=FakeKeys(keys.public_key()))
    app.get("/")(lambda: {"page": True})
    app.get("/healthz")(lambda: {"ok": True})
    return TestClient(app)


def token(key, **claims):
    body = {"aud": AUD, "iss": TEAM_DOMAIN, "exp": int(time.time()) + 60} | claims
    return jwt.encode(body, key, algorithm="RS256")


def test_signed_pass_gets_in(client, keys):
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(keys)}).status_code == 200


def test_no_pass_is_refused(client):
    assert client.get("/").status_code == 403


@pytest.mark.parametrize("claims", [{"aud": "another-app"}, {"iss": "https://evil.example"},
                                    {"exp": int(time.time()) - 60}])
def test_wrong_or_expired_pass_is_refused(client, keys, claims):
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(keys, **claims)}).status_code == 403


def test_pass_signed_by_someone_else_is_refused(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token(other)}).status_code == 403


def test_health_check_needs_no_pass(client):
    assert client.get("/healthz").status_code == 200
