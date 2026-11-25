"""Who the hub answers, whatever sits in front of it.

- Only addresses it knows. Without that, a web page you visit could point its own
  domain name at your hub's local address and read it (DNS rebinding).
- With a password saved, only requests that carry it.
- Opened at an address other than this machine's, only with a sign-in: the hub's
  password, Cloudflare Access, or another sign-in service you declare.
- Each page tells the browser to run only the hub's own scripts and never to show
  the hub inside another site.
"""
import base64
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The container's own health check; it shows nothing.
OPEN_PATHS = {"/healthz"}
MIN_PASSWORD = 12
# After this many wrong passwords from one address, it waits out the window.
MAX_FAILURES, WINDOW = 10, 300
FAILURES: dict[str, list[float]] = {}  # address -> when it got the password wrong
HEADERS = {
    # Images may come from the stores (app icons); everything else only from the hub.
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' https: data:; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def allowed_hosts() -> set[str]:
    """This machine, the host of ASO_HUB_URL, and any in ASO_HUB_ALLOWED_HOSTS ("*" allows all)."""
    hosts = set(LOCAL_HOSTS)
    own = urlsplit(os.environ.get("ASO_HUB_URL", "")).hostname
    if own:
        hosts.add(own)
    extra = os.environ.get("ASO_HUB_ALLOWED_HOSTS", "")
    return hosts | {h.strip().lower() for h in extra.split(",") if h.strip()}


def password() -> str | None:
    """The saved password, read each time so a new one applies at once. A file that
    exists but can't be read raises, so a permissions mistake never opens the hub."""
    name = os.environ.get("HUB_PASSWORD_FILE")
    if not name:
        return None
    try:
        return Path(name).read_text().strip() or None
    except FileNotFoundError:
        return None


def signs_in_elsewhere() -> bool:
    declared = os.environ.get("ASO_HUB_BEHIND_SIGN_IN", "").strip().lower() in {"1", "true", "yes"}
    return declared or bool(os.environ.get("CF_ACCESS_AUD"))


def _given_password(header: str) -> str | None:
    """The password in a browser's Basic sign-in; the user name can be anything."""
    kind, _, value = header.partition(" ")
    if kind.lower() != "basic":
        return None
    try:
        return base64.b64decode(value.strip(), validate=True).decode().partition(":")[2]
    except ValueError:
        return None


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = self._refusal(request) or await call_next(request)
        response.headers.update(HEADERS)
        return response

    def _refusal(self, request: Request) -> PlainTextResponse | None:
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        allowed = allowed_hosts()
        if "*" not in allowed and host not in allowed:
            return PlainTextResponse(
                "The hub doesn't answer to this address. Set ASO_HUB_URL to the address you open it at, "
                "or list it in ASO_HUB_ALLOWED_HOSTS.", status_code=400)
        if request.url.path in OPEN_PATHS:
            return None
        secret = password()
        if secret is None:
            # Judged on the settings, not the request, so a made-up "localhost" doesn't get past.
            if allowed - LOCAL_HOSTS and not signs_in_elsewhere():
                return PlainTextResponse(
                    "This hub is set up to be opened from other machines, so it needs a sign-in. Save a password "
                    "as the hub-password secret, or, if a proxy already signs people in, set "
                    "ASO_HUB_BEHIND_SIGN_IN=true.", status_code=503)
            return None
        if len(secret) < MIN_PASSWORD:
            return PlainTextResponse(
                f"The saved password is shorter than {MIN_PASSWORD} characters. Save a longer one.", status_code=503)
        return self._check(request, secret)

    def _check(self, request: Request, secret: str) -> PlainTextResponse | None:
        who = request.client.host if request.client else "?"
        now = time.monotonic()
        recent = [t for t in FAILURES.get(who, []) if now - t < WINDOW]
        if len(recent) >= MAX_FAILURES:
            return PlainTextResponse("Too many wrong passwords. Try again in a few minutes.", status_code=429)
        given = _given_password(request.headers.get("authorization", ""))
        if given is not None and hmac.compare_digest(given.encode(), secret.encode()):
            FAILURES.pop(who, None)
            return None
        if given is not None:  # a first visit with no password yet isn't a wrong guess
            if len(FAILURES) > 1000:
                FAILURES.clear()
            FAILURES[who] = [*recent, now]
        return PlainTextResponse("Enter the hub's password.", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="ASO Hub", charset="UTF-8"'})
