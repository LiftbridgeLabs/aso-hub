"""Check Cloudflare Access's signed sign-in pass on every request.

Cloudflare Access already stops strangers at the edge. This also stops anything
that reaches the container another way (another container on the same Docker
network), because only Cloudflare can sign the pass. Off when CF_ACCESS_AUD is
unset, which is how the hub runs on the Mac.
"""
import os

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Your Cloudflare Zero Trust team domain, e.g. https://yourteam.cloudflareaccess.com
TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
# The container's own health check calls this from inside; it shows nothing.
OPEN_PATHS = {"/healthz"}


class AccessCheck(BaseHTTPMiddleware):
    def __init__(self, app, audience: str | None = None, team_domain: str = TEAM_DOMAIN,
                 jwks: jwt.PyJWKClient | None = None):
        super().__init__(app)
        self.audience = audience if audience is not None else os.environ.get("CF_ACCESS_AUD")
        self.team_domain = team_domain
        self.jwks = jwks or (jwt.PyJWKClient(f"{team_domain}/cdn-cgi/access/certs") if self.audience else None)

    async def dispatch(self, request: Request, call_next):
        if not self.audience or request.url.path in OPEN_PATHS:
            return await call_next(request)
        token = request.headers.get("cf-access-jwt-assertion")
        if not token:
            return PlainTextResponse("Sign in through Cloudflare Access.", status_code=403)
        try:
            # Fetching Cloudflare's keys (cached after the first time) is a blocking call.
            key = (await run_in_threadpool(self.jwks.get_signing_key_from_jwt, token)).key
            jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.team_domain)
        except jwt.PyJWTError:
            return PlainTextResponse("Sign in through Cloudflare Access.", status_code=403)
        return await call_next(request)
