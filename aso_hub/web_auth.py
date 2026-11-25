"""The sign-in pages, and Settings → Sign-in."""
import logging
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import auth, crypto, db, forms, guard, oidc
from .ui import conn_and_config, hub_url, page, saved, templates

router = APIRouter()
log = logging.getLogger("aso_hub.auth")
OIDC_COOKIE = "aso_hub_oidc"
CALLBACK = "/auth/oidc/callback"


def _address(request: Request) -> str:
    return request.client.host if request.client else "?"


def _secure(request: Request) -> bool:
    own = urlsplit(hub_url(request))
    return request.url.scheme == "https" or (own.scheme == "https" and own.hostname == request.url.hostname)


def _cookie(response, request: Request, name: str, value: str, seconds: int) -> None:
    response.set_cookie(name, value, max_age=seconds, httponly=True, samesite="lax", secure=_secure(request), path="/")


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _signed_in_response(request: Request, conn: sqlite3.Connection, to: str = "/") -> RedirectResponse:
    response = _go(to)
    _cookie(response, request, guard.SESSION_COOKIE, auth.start_session(conn), auth.SESSION_SECONDS)
    return response


def _auth_page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
    """A page for someone not signed in: nothing about the apps on it."""
    return templates.TemplateResponse(request, name, context, status_code=status)


def _login_page(request: Request, conn: sqlite3.Connection, error: str = "", status: int = 200):
    return _auth_page(request, "login.html", status, error=error, providers=oidc.providers(conn, enabled_only=True))


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    _, conn = conn_and_config()
    with closing(conn):
        if guard.cloudflare_access() or auth.account(conn) is not None:
            return _go("/")
        print(f"ASO Hub setup code: {auth.setup_code(conn)}", flush=True)
        return _auth_page(request, "setup.html", error="")


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request):
    form = await forms.read(request)

    def run():
        _, conn = conn_and_config()
        with closing(conn):
            if guard.cloudflare_access():
                return _go("/")
            try:
                auth.first_account(conn, form.text("code"), form.secret("password"), form.secret("again"),
                                   _address(request))
            except auth.AuthError as e:
                return _auth_page(request, "setup.html", 400, error=str(e))
            return _signed_in_response(request, conn)

    return await run_in_threadpool(run)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    _, conn = conn_and_config()
    with closing(conn):
        if request.state.signed_in:
            return _go("/")
        if auth.account(conn) is None:
            return _go("/setup")
        return _login_page(request, conn)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await forms.read(request)

    def run():
        _, conn = conn_and_config()
        with closing(conn):
            try:
                auth.check_password(conn, form.secret("password"), _address(request))
            except auth.AuthError as e:
                return _login_page(request, conn, str(e), 401)
            if auth.two_step_on(conn):
                response = _go("/login/code")
                _cookie(response, request, guard.SESSION_COOKIE, auth.start_session(conn, pending=True),
                        auth.PENDING_SECONDS)
                return response
            auth.right(_address(request))
            return _signed_in_response(request, conn)

    return await run_in_threadpool(run)


@router.get("/login/code", response_class=HTMLResponse)
def code_form(request: Request):
    _, conn = conn_and_config()
    with closing(conn):
        if not auth.session_ok(conn, request.cookies.get(guard.SESSION_COOKIE), pending=True):
            return _go("/login")
        return _auth_page(request, "code.html", error="")


@router.post("/login/code", response_class=HTMLResponse)
async def code_submit(request: Request):
    form = await forms.read(request)

    def run():
        cfg, conn = conn_and_config()
        with closing(conn):
            waiting = request.cookies.get(guard.SESSION_COOKIE)
            if not auth.session_ok(conn, waiting, pending=True):
                return _go("/login")
            try:
                auth.check_code(conn, cfg.database_path, form.text("code"), _address(request))
            except auth.AuthError as e:
                return _auth_page(request, "code.html", 401, error=str(e))
            auth.right(_address(request))
            auth.end_session(conn, waiting)
            return _signed_in_response(request, conn)

    return await run_in_threadpool(run)


@router.post("/logout")
def logout(request: Request):
    _, conn = conn_and_config()
    with closing(conn):
        auth.end_session(conn, request.cookies.get(guard.SESSION_COOKIE))
    response = _go("/login")
    response.delete_cookie(guard.SESSION_COOKIE, path="/")
    return response


# Single sign-on

@router.get(CALLBACK, response_class=HTMLResponse)
def oidc_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    cfg, conn = conn_and_config()
    with closing(conn):
        try:
            if not state or state != request.cookies.get(OIDC_COOKIE):
                raise oidc.OidcError("That sign-in didn't start in this browser. Start again.")
            waiting = oidc.take_state(conn, state)
            trying = bool(waiting["trying"]) and request.state.signed_in
            if error or not code:
                raise oidc.OidcError(f"The provider didn't sign you in ({error[:100] or 'no code'}).")
            with oidc.client() as http:
                email = oidc.finish(conn, cfg.database_path, waiting, code, hub_url(request) + CALLBACK, http)
            allowed = oidc.is_allowed(email, oidc.allowed_list(conn))
            if trying:
                response = _auth_page(request, "oidc_result.html", email=email, allowed=allowed, error="")
            elif allowed:
                response = _signed_in_response(request, conn)
            else:
                log.warning("single sign-on refused: an address not on the allowed list")
                raise oidc.OidcError(f"{email} isn't on this hub's allowed list.")
        except oidc.OidcError as e:
            if request.state.signed_in:
                response = _auth_page(request, "oidc_result.html", 400, email="", allowed=False, error=str(e))
            else:
                response = _login_page(request, conn, str(e), 401)
        response.delete_cookie(OIDC_COOKIE, path="/")
        return response


@router.get("/auth/oidc/{provider_id}")
def oidc_start(request: Request, provider_id: int, test: bool = False):
    _, conn = conn_and_config()
    with closing(conn):
        try:
            with oidc.client() as http:
                address, state = oidc.begin(conn, provider_id, hub_url(request) + CALLBACK,
                                            test and request.state.signed_in, http)
        except oidc.OidcError as e:
            return _login_page(request, conn, str(e), 400)
    response = _go(address)
    _cookie(response, request, OIDC_COOKIE, state, oidc.STATE_SECONDS)
    return response


# Settings → Sign-in

def _sign_in_page(request: Request, cfg, conn, status: int = 200, **context):
    row = auth.account(conn)
    return page(request, "settings/sign_in.html", cfg, conn, code=status, account=row is not None,
                two_step=bool(row and row["totp_on"]), providers=oidc.providers(conn),
                allowed=oidc.allowed_list(conn), callback=hub_url(request) + CALLBACK,
                access=guard.cloudflare_access(), hosts=sorted(guard.allowed_hosts()), **context)


@router.get("/settings/sign-in", response_class=HTMLResponse)
def sign_in_settings(request: Request):
    cfg, conn = conn_and_config()
    with closing(conn):
        return _sign_in_page(request, cfg, conn)


def _qr(link: str) -> str:
    return segno.make(link, error="m").svg_inline(scale=4, border=2, dark="#000", light="#fff")


def _confirm(conn: sqlite3.Connection, form: forms.Form, request: Request) -> None:
    """Changes to who can get in ask for the password again, when there is one."""
    if auth.account(conn) is not None:
        auth.check_password(conn, form.secret("current"), _address(request))


def _change(request: Request, cfg, conn: sqlite3.Connection, action: str, form: forms.Form):
    token = request.cookies.get(guard.SESSION_COOKIE)
    if action == "password":
        had = auth.account(conn) is not None
        _confirm(conn, form, request)
        auth.check_new_password(form.secret("password"), form.secret("again"))
        auth.set_password(conn, form.secret("password"))  # ends every session, this one too
        response = saved("/settings/sign-in", "Password saved." if had else "Password created.")
        if token:
            _cookie(response, request, guard.SESSION_COOKIE, auth.start_session(conn), auth.SESSION_SECONDS)
        return response
    if action == "two-step-start":
        _confirm(conn, form, request)
        secret, link = auth.begin_two_step(conn, cfg.database_path, token)
        return _sign_in_page(request, cfg, conn, setup={"secret": secret, "qr": _qr(link)})
    if action == "two-step-confirm":
        try:
            codes = auth.confirm_two_step(conn, cfg.database_path, form.text("code"), token)
        except auth.AuthError as e:
            secret, link = auth.pending_two_step(conn, cfg.database_path, token)
            return _sign_in_page(request, cfg, conn, status=400, error=str(e),
                                 setup={"secret": secret, "qr": _qr(link)})
        return _sign_in_page(request, cfg, conn, codes=codes)
    if action == "two-step-codes":
        _confirm(conn, form, request)
        if not auth.two_step_on(conn):
            raise auth.AuthError("Two-step sign-in isn't on.")
        return _sign_in_page(request, cfg, conn, codes=auth.new_recovery_codes(conn))
    if action == "two-step-off":
        _confirm(conn, form, request)
        auth.turn_off_two_step(conn)
        return saved("/settings/sign-in", "Two-step sign-in is off.")
    if action == "allowed":
        _confirm(conn, form, request)
        db.set_setting(conn, "allowed_emails", oidc.parse_allowed(form.text("allowed")))
        auth.end_other_sessions(conn, token)  # someone just removed from the list is signed out
        return saved("/settings/sign-in", "Allowed list saved.")
    if action == "provider":
        _confirm(conn, form, request)
        _save_provider(cfg, conn, form)
        auth.end_other_sessions(conn, token)
        return saved("/settings/sign-in", "Sign-in provider saved. Use Try it to check it.")
    if action == "provider-remove":
        _confirm(conn, form, request)
        with conn:
            conn.execute("DELETE FROM sign_in_providers WHERE id = ?", (form.text("id"),))
        auth.end_other_sessions(conn, token)
        return saved("/settings/sign-in", "Sign-in provider removed.")
    raise auth.AuthError("Unknown change.")


def _save_provider(cfg, conn: sqlite3.Connection, form: forms.Form) -> None:
    name, client_id, secret = form.text("name"), form.text("client_id"), form.secret("client_secret").strip()
    issuer = oidc.check_issuer_address(form.text("issuer"))
    if not name or not client_id:
        raise oidc.OidcError("Give the provider a name and a client ID.")
    old = oidc.provider(conn, int(form.text("id"))) if form.text("id").isdigit() else None
    # A saved secret is only ever sent to the issuer it was saved for.
    if not secret and (old is None or old["issuer"] != issuer):
        raise oidc.OidcError("Enter the client secret.")
    with oidc.client() as http:
        oidc.discover(http, issuer)
    sealed = crypto.encrypt(cfg.database_path, secret) if secret else old["client_secret"]
    values = (name[:60], issuer, client_id, sealed, int(form.on("enabled")), int(form.on("trust_email")))
    with conn:
        if old is None:
            conn.execute("INSERT INTO sign_in_providers (name, issuer, client_id, client_secret, enabled, trust_email) "
                         "VALUES (?, ?, ?, ?, ?, ?)", values)
        else:
            conn.execute("UPDATE sign_in_providers SET name = ?, issuer = ?, client_id = ?, client_secret = ?, "
                         "enabled = ?, trust_email = ? WHERE id = ?", (*values, old["id"]))


@router.post("/settings/sign-in/{action}", response_class=HTMLResponse)
async def sign_in_change(request: Request, action: str):
    form = await forms.read(request)

    def run():
        cfg, conn = conn_and_config()
        with closing(conn):
            try:
                return _change(request, cfg, conn, action, form)
            except (auth.AuthError, oidc.OidcError) as e:
                return _sign_in_page(request, cfg, conn, status=400, error=str(e))

    return await run_in_threadpool(run)
