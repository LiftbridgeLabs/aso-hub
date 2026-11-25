"""Who the hub answers.

- Only addresses it knows: this machine, any IP address, and the names you give
  it. Without that, a web page you visit could point its own domain name at your
  hub's local address and reach it (DNS rebinding).
- Changes only from the hub's own pages, so another website can't submit one
  through your browser.
- Pages only to someone signed in: with the account, a single sign-on, or
  Cloudflare Access.
- Each page tells the browser to run only the hub's own scripts and never to show
  the hub inside another site.
"""
import ipaddress
import os
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from . import auth, db, settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The container's own health check; it shows nothing.
OPEN_PATHS = {"/healthz"}
# What someone not signed in yet has to reach: the sign-in pages and their styles.
SIGN_IN_PATHS = ("/static/", "/setup", "/login", "/auth/oidc/")
SESSION_COOKIE = "aso_hub_session"
READS = {"GET", "HEAD", "OPTIONS"}
HEADERS = {
    # Images may come from the stores (app icons); everything else only from the hub.
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' https: data:; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not "no-referrer": that would hide where the hub's own forms come from.
    "Referrer-Policy": "same-origin",
}


def is_address(host: str | None) -> bool:
    """Whether the browser was given a number (192.168.1.20), which nobody else's domain can stand in for."""
    try:
        ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return True


def allowed_hosts() -> set[str]:
    """Names the hub answers to: localhost, the host of ASO_HUB_URL, and any in ASO_HUB_ALLOWED_HOSTS
    ("*" allows all). IP addresses are always answered."""
    hosts = set(LOCAL_HOSTS)
    own = urlsplit(os.environ.get("ASO_HUB_URL", "")).hostname
    if own:
        hosts.add(own)
    extra = os.environ.get("ASO_HUB_ALLOWED_HOSTS", "")
    return hosts | {h.strip().lower() for h in extra.split(",") if h.strip()}


def cloudflare_access() -> bool:
    return bool(os.environ.get("CF_ACCESS_AUD"))


def from_own_pages(request: Request) -> bool:
    """Whether the browser says this request comes from one of the hub's own pages."""
    site = request.headers.get("sec-fetch-site")
    if site:  # sent over https and on localhost, and a page can't forge it
        return site in {"same-origin", "none"}
    origin = urlsplit(request.headers.get("origin", ""))
    return bool(origin.netloc) and origin.netloc == request.headers.get("host")


class Guard(BaseHTTPMiddleware):
    """Outermost: the address, where changes come from, and the browser protections."""

    async def dispatch(self, request: Request, call_next):
        response = self._refusal(request) or await call_next(request)
        response.headers.update(HEADERS)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _refusal(self, request: Request) -> PlainTextResponse | None:
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        allowed = allowed_hosts()
        if "*" not in allowed and host not in allowed and not is_address(host):
            return PlainTextResponse(
                "The hub doesn't answer to this address. Set ASO_HUB_URL to the address you open it at, "
                "or list it in ASO_HUB_ALLOWED_HOSTS.", status_code=400)
        if request.method not in READS and not from_own_pages(request):
            return PlainTextResponse("That change didn't come from the hub's own pages.", status_code=403)
        return None


def _signed_in(token: str | None) -> tuple[bool, bool]:
    """(signed in, an account exists)"""
    conn = db.connect(settings.load().database_path)
    try:
        if auth.account(conn) is None:
            auth.import_password_file(conn)
        return auth.session_ok(conn, token), auth.account(conn) is not None
    finally:
        conn.close()


class SignedIn(BaseHTTPMiddleware):
    """Innermost, so Cloudflare Access, when set up, has already checked its own pass."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if cloudflare_access():
            request.state.signed_in = True
            return await call_next(request)
        signed, exists = await run_in_threadpool(_signed_in, request.cookies.get(SESSION_COOKIE))
        request.state.signed_in = signed
        if signed or path.startswith(SIGN_IN_PATHS):
            return await call_next(request)
        if request.method not in READS:
            return PlainTextResponse("Sign in first.", status_code=401)
        return RedirectResponse("/login" if exists else "/setup", status_code=303)
