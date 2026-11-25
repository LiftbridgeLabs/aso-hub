"""Two protections on every answer, whatever sits in front of the hub.

The hub only answers to addresses it knows. Without that, a web page you visit
could point its own domain name at your hub's local address and read it (DNS
rebinding). And each page tells the browser to run only the hub's own scripts and
never to show the hub inside another site.
"""
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
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


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            host = None
        allowed = allowed_hosts()
        if "*" in allowed or host in allowed:
            response = await call_next(request)
        else:
            response = PlainTextResponse(
                "The hub doesn't answer to this address. Set ASO_HUB_URL to the address you open it at, "
                "or list it in ASO_HUB_ALLOWED_HOSTS.", status_code=400)
        response.headers.update(HEADERS)
        return response
