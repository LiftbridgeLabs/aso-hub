"""Single sign-on with any OpenID Connect provider (Pocket ID, Authentik, Authelia,
Google and others): the authorization code flow with PKCE.

Only the addresses on the allowed list may sign in, whatever the provider says.
"""
import base64
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from . import crypto, db

STATE_SECONDS = 600
SCOPES = "openid email profile"
# Signed with the provider's private key only: never "none", never a shared-secret method.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
DISCOVERY = "/.well-known/openid-configuration"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class OidcError(Exception):
    """A problem with a single sign-on, in plain words."""


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=15, transport=transport, headers={"User-Agent": "aso-hub"})


def check_issuer_address(issuer: str) -> str:
    issuer = issuer.strip()
    if issuer.endswith(DISCOVERY):
        issuer = issuer[: -len(DISCOVERY)]
    parts = urlsplit(issuer)
    if not parts.hostname or parts.query or parts.fragment or not _private_enough(issuer):
        raise OidcError("The issuer must be an https address, such as https://id.example.com.")
    return issuer


def _json(response: httpx.Response, what: str) -> dict:
    if response.status_code != 200:
        raise OidcError(f"The provider answered {response.status_code} for {what}.")
    try:
        body = response.json()
    except ValueError:
        raise OidcError(f"The provider's {what} wasn't readable.") from None
    if not isinstance(body, dict):
        raise OidcError(f"The provider's {what} wasn't readable.")
    return body


def discover(http: httpx.Client, issuer: str) -> dict:
    try:
        found = _json(http.get(issuer.rstrip("/") + DISCOVERY), "its setup document")
    except httpx.HTTPError as e:
        raise OidcError(f"Couldn't reach the provider ({type(e).__name__}).") from None
    if str(found.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        raise OidcError("The provider names a different issuer than the address saved.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not found.get(key):
            raise OidcError(f"The provider's setup document has no {key}.")
        check_endpoint(found[key])
    return found


def _private_enough(address: str) -> bool:
    """https, or http that never leaves this machine."""
    parts = urlsplit(address)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


def check_endpoint(address: str) -> None:
    """The provider's own addresses are held to the same rule as the issuer's."""
    if not _private_enough(address):
        raise OidcError("The provider points to an address that isn't https.")


def providers(conn: sqlite3.Connection, enabled_only: bool = False) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM sign_in_providers ORDER BY id").fetchall()
    return [r for r in rows if r["enabled"] or not enabled_only]


def provider(conn: sqlite3.Connection, provider_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sign_in_providers WHERE id = ?", (provider_id,)).fetchone()
    if row is None:
        raise OidcError("That sign-in provider isn't set up.")
    return row


def allowed_list(conn: sqlite3.Connection) -> list[str]:
    return db.get_setting(conn, "allowed_emails", [])


def parse_allowed(text: str) -> list[str]:
    """Addresses, or "@example.com" for a whole domain, separated by commas, spaces or lines."""
    items = text.replace(",", " ").split()
    out = []
    for item in (i.strip().lower() for i in items):
        local, at, domain = item.partition("@")
        if not at or "." not in domain or "@" in domain:
            raise OidcError(f"“{item}” isn't an email address or an @domain.")
        out.append(item if local else f"@{domain}")
    return list(dict.fromkeys(out))


def is_allowed(email: str, allowed: list[str]) -> bool:
    email = email.strip().lower()
    _, at, domain = email.rpartition("@")
    return bool(at) and (email in allowed or f"@{domain}" in allowed)


def begin(conn: sqlite3.Connection, provider_id: int, redirect_uri: str, trying: bool,
          http: httpx.Client) -> tuple[str, str]:
    """The address to send the browser to, and the state to remember in its cookie."""
    row = provider(conn, provider_id)
    if not row["enabled"] and not trying:
        raise OidcError("That sign-in provider is turned off.")
    found = discover(http, row["issuer"])
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with conn:
        conn.execute("DELETE FROM oidc_states WHERE expires < ?", (time.time(),))
        # Anyone can start a sign-in, so the waiting ones are capped.
        conn.execute("DELETE FROM oidc_states WHERE state IN "
                     "(SELECT state FROM oidc_states ORDER BY expires DESC LIMIT -1 OFFSET 200)")
        conn.execute("INSERT INTO oidc_states VALUES (?, ?, ?, ?, ?, ?)",
                     (state, provider_id, nonce, verifier, int(trying), time.time() + STATE_SECONDS))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({"response_type": "code", "client_id": row["client_id"], "redirect_uri": redirect_uri,
                       "scope": SCOPES, "state": state, "nonce": nonce, "code_challenge": challenge,
                       "code_challenge_method": "S256"})
    joiner = "&" if "?" in found["authorization_endpoint"] else "?"
    return found["authorization_endpoint"] + joiner + query, state


def take_state(conn: sqlite3.Connection, state: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM oidc_states WHERE state = ? AND expires > ?", (state, time.time())).fetchone()
    with conn:
        used = conn.execute("DELETE FROM oidc_states WHERE state = ?", (state,)).rowcount
    if row is None or not used:
        raise OidcError("That sign-in took too long or was already used. Start again.")
    return row


def _verify(token: str, keys: dict, issuer: str, client_id: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        usable = [k for k in jwt.PyJWKSet.from_dict(keys).keys
                  if k.public_key_use in (None, "sig") and header.get("kid") in (None, k.key_id)]
        if len(usable) != 1:
            raise OidcError("The provider's signing key wasn't found.")
        claims = jwt.decode(token, usable[0].key, algorithms=ALGORITHMS, audience=client_id, issuer=issuer,
                            leeway=60, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.PyJWTError as e:
        raise OidcError(f"The provider's answer couldn't be trusted ({type(e).__name__}).") from None
    if not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
        raise OidcError("The provider's answer belongs to another sign-in. Start again.")
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != client_id:
        raise OidcError("The provider's answer was meant for another app.")
    return claims


def finish(conn: sqlite3.Connection, database: Path, state: sqlite3.Row, code: str, redirect_uri: str,
           http: httpx.Client) -> str:
    """Swap the code for the provider's signed answer and return the person's verified email."""
    row = provider(conn, state["provider_id"])
    secret = crypto.decrypt(database, row["client_secret"])
    if secret is None:
        raise OidcError("The saved client secret can't be opened. Enter it again in Settings.")
    found = discover(http, row["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": state["verifier"]}
    methods = found.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    auth = None
    if "client_secret_basic" in methods:
        auth = (row["client_id"], secret)
    else:
        data |= {"client_id": row["client_id"], "client_secret": secret}
    try:
        tokens = _json(http.post(found["token_endpoint"], data=data, auth=auth), "the sign-in code")
        if not tokens.get("id_token"):
            raise OidcError("The provider sent no ID token.")
        keys = _json(http.get(found["jwks_uri"]), "its signing keys")
        claims = _verify(tokens["id_token"], keys, found["issuer"], row["client_id"], state["nonce"])
        incomplete = not claims.get("email") or "email_verified" not in claims
        if incomplete and found.get("userinfo_endpoint") and tokens.get("access_token"):
            check_endpoint(found["userinfo_endpoint"])
            info = _json(http.get(found["userinfo_endpoint"],
                                  headers={"Authorization": f"Bearer {tokens['access_token']}"}), "your details")
            if info.get("sub") != claims["sub"]:
                raise OidcError("The provider's details belong to someone else.")
            # A "verified" counts only for the address it came with.
            if info.get("email") and claims.get("email") in (None, info["email"]):
                claims = claims | {k: info[k] for k in ("email", "email_verified") if k in info}
    except httpx.HTTPError as e:
        raise OidcError(f"Couldn't reach the provider ({type(e).__name__}).") from None
    if not claims.get("email"):
        raise OidcError("The provider didn't share an email address.")
    if claims.get("email_verified") not in (True, "true") and not row["trust_email"]:
        raise OidcError("The provider didn't say that email address is verified. If you control the provider and "
                        "trust its addresses, tick that for it under Settings → Sign-in.")
    return str(claims["email"])
