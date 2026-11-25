import re
import time

import pytest
from fastapi.testclient import TestClient

from aso_hub import auth, db, guard, totp, web
from aso_hub.__main__ import main

PASSWORD = "correct horse battery"  # noqa: S105 (a test password)
pytestmark = pytest.mark.real_sign_in
OWN = {"sec-fetch-site": "same-origin"}


@pytest.fixture
def hub(cfg, monkeypatch):
    monkeypatch.setattr(auth, "FAILURES", {})
    return TestClient(web.app, headers=OWN, follow_redirects=False)


@pytest.fixture
def conn_(cfg):
    c = db.connect(cfg.database_path)
    yield c
    c.close()


@pytest.fixture
def owner(hub, conn_):
    """A hub with its account made, and this browser signed in."""
    r = hub.post("/setup", data={"code": auth.setup_code(conn_), "password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return hub


def test_a_new_hub_shows_only_the_setup_page(hub):
    for path in ("/", "/settings", "/apps/lumen-ios", "/actions"):
        r = hub.get(path)
        assert (r.status_code, r.headers["location"]) == (303, "/setup"), path
    page = hub.get("/setup").text
    assert "Create your account" in page and "Lumen" not in page
    assert hub.get("/healthz").status_code == 200
    assert hub.get("/static/hub.css").status_code == 200


def test_the_account_needs_the_setup_code(hub, conn_):
    r = hub.post("/setup", data={"code": "guess", "password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 400 and auth.account(conn_) is None


@pytest.mark.parametrize("password, again", [("short", "short"), (PASSWORD, PASSWORD + "x")])
def test_a_weak_or_mistyped_password_is_refused(hub, conn_, password, again):
    r = hub.post("/setup", data={"code": auth.setup_code(conn_), "password": password, "again": again})
    assert r.status_code == 400 and auth.account(conn_) is None


def test_only_one_account_can_ever_be_made(owner, conn_):
    other = TestClient(web.app, headers=OWN, follow_redirects=False)
    r = other.post("/setup", data={"code": "x", "password": "another password!", "again": "another password!"})
    assert r.status_code == 400
    assert auth.password_matches(PASSWORD, auth.account(conn_)["password_hash"])


def test_sign_in_sign_out(owner):
    assert owner.get("/").status_code == 200
    cookie = owner.cookies.get(guard.SESSION_COOKIE)
    assert owner.post("/logout").status_code == 303
    owner.cookies.set(guard.SESSION_COOKIE, cookie)  # a stolen copy is dead after signing out
    assert owner.get("/").headers["location"] == "/login"
    owner.cookies.clear()
    assert owner.post("/login", data={"password": "wrong password!!"}).status_code == 401
    r = owner.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "samesite=lax" in flags
    assert owner.get("/").status_code == 200


def test_the_cookie_is_https_only_on_an_https_address(hub, conn_, monkeypatch):
    monkeypatch.setenv("ASO_HUB_URL", "https://hub.example.com")
    r = hub.post("/setup", headers={"host": "hub.example.com"},
                 data={"code": auth.setup_code(conn_), "password": PASSWORD, "again": PASSWORD})
    assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_passwords_pause_sign_in(owner):
    owner.cookies.clear()
    for _ in range(auth.PER_ADDRESS):
        owner.post("/login", data={"password": "nope nope nope"})
    r = owner.post("/login", data={"password": PASSWORD})
    assert r.status_code == 401 and "Too many wrong tries" in r.text


@pytest.mark.parametrize("headers", [{"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                                     {"origin": "https://evil.example"}, {"origin": "null"}, {}])
def test_another_website_cant_submit_changes(owner, headers):
    other = TestClient(web.app, headers=headers, follow_redirects=False, cookies=owner.cookies)
    r = other.post("/settings/schedule/pull")
    assert r.status_code == 403


def test_a_browser_that_only_sends_origin_can_still_submit(owner):
    plain = TestClient(web.app, headers={"origin": "http://testserver"}, follow_redirects=False, cookies=owner.cookies)
    assert plain.post("/logout").status_code == 303


def test_changes_need_a_signed_in_browser(owner):
    stranger = TestClient(web.app, headers=OWN, follow_redirects=False)
    for path in ("/settings/connections/save", "/settings/sign-in/password", "/settings/schedule/pull"):
        assert stranger.post(path).status_code == 401, path


def test_two_step_sign_in(owner, conn_, cfg, monkeypatch):
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now)  # a 30-second step can't pass mid-test
    assert owner.post("/settings/sign-in/two-step-start", data={"current": "wrong wrong wrong"}).status_code == 400
    page = owner.post("/settings/sign-in/two-step-start", data={"current": PASSWORD}).text
    secret = re.search(r"<code>([A-Z2-7]{32})</code>", page).group(1)
    assert "<svg" in page
    assert not auth.two_step_on(conn_)  # not on until a code proves the app has it
    step = totp.step_of(time.time())
    done = owner.post("/settings/sign-in/two-step-confirm", data={"code": totp.code_at(secret, step - 1)})
    codes = re.findall(r"<code>([a-z2-7]{5}-[a-z2-7]{5})</code>", done.text)
    assert len(codes) == 10 and auth.two_step_on(conn_)

    owner.post("/logout")
    owner.cookies.clear()
    r = owner.post("/login", data={"password": PASSWORD})
    assert r.headers["location"] == "/login/code"
    assert owner.get("/").headers["location"] == "/login"  # a password alone opens nothing
    assert owner.post("/login/code", data={"code": totp.code_at(secret, step - 1)}).status_code == 401  # used once
    assert owner.post("/login/code", data={"code": totp.code_at(secret, step)}).status_code == 303
    assert owner.get("/").status_code == 200

    owner.cookies.clear()
    owner.post("/login", data={"password": PASSWORD})
    assert owner.post("/login/code", data={"code": codes[0].upper()}).status_code == 303
    owner.cookies.clear()
    owner.post("/login", data={"password": PASSWORD})
    assert owner.post("/login/code", data={"code": codes[0]}).status_code == 401  # a recovery code works once


def test_only_the_browser_that_gave_the_password_can_finish_two_step_setup(owner, conn_):
    owner.post("/settings/sign-in/two-step-start", data={"current": PASSWORD})
    thief = TestClient(web.app, headers=OWN, follow_redirects=False)
    thief.cookies.set(guard.SESSION_COOKIE, auth.start_session(conn_))  # signed in, without the password
    r = thief.post("/settings/sign-in/two-step-confirm", data={"code": "000000"})
    assert r.status_code == 400 and "Start setting up two-step sign-in again" in r.text
    assert not re.search(r"<code>[A-Z2-7]{32}</code>", r.text) and "<svg viewBox" not in r.text.split("<main>")[1]


def test_changing_who_may_sign_in_signs_other_browsers_out(owner, conn_):
    other = auth.start_session(conn_)
    owner.post("/settings/sign-in/allowed", data={"current": PASSWORD, "allowed": "me@example.com"})
    assert not auth.session_ok(conn_, other)
    assert owner.get("/").status_code == 200


def test_the_code_step_cant_be_reached_without_the_password(owner):
    owner.cookies.clear()
    assert owner.get("/login/code").headers["location"] == "/login"
    assert owner.post("/login/code", data={"code": "123456"}).headers["location"] == "/login"


def test_changing_the_password_signs_other_browsers_out(owner):
    other = TestClient(web.app, headers=OWN, follow_redirects=False)
    other.post("/login", data={"password": PASSWORD})
    assert other.get("/").status_code == 200
    r = owner.post("/settings/sign-in/password",
                   data={"current": PASSWORD, "password": "a brand new password", "again": "a brand new password"})
    assert r.status_code == 303
    assert owner.get("/").status_code == 200
    assert other.get("/").status_code == 303


def test_a_password_file_from_an_older_version_becomes_the_account(hub, conn_, tmp_path, monkeypatch):
    (tmp_path / "hub-password").write_text(PASSWORD + "\n")
    monkeypatch.setenv("HUB_PASSWORD_FILE", str(tmp_path / "hub-password"))
    assert hub.get("/").headers["location"] == "/login"
    assert hub.post("/login", data={"password": PASSWORD}).status_code == 303


def test_the_server_can_reset_a_lost_password(owner, conn_, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt: "reset from the server")
    assert main(["reset-password"]) == 0
    assert owner.get("/").status_code == 303
    assert main(["turn-off-two-step"]) == 0
    assert owner.post("/login", data={"password": "reset from the server"}).status_code == 303


def test_cloudflare_access_counts_as_signed_in(cfg, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_AUD", "aud")
    assert guard.cloudflare_access()


def test_a_huge_form_is_refused(owner):
    assert owner.post("/settings/apps/add", data={"name": "x" * 200_000}).status_code == 413
