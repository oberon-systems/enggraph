"""Log people in, and answer the gateway's question of who a request is from.

The gateway asks `/auth/check` before it passes any request on, so the check
lives here and not in the identity provider: any OIDC issuer will do. With no
issuer configured the login form takes an operator token instead.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client, OAuthError
from fastapi import APIRouter, Request
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from enggraph.core import authn
from enggraph.web.args import API_TOKEN
from enggraph.web.pages import render

log = logging.getLogger(__name__)

SESSION_COOKIE = "eg_session"
LOGIN_COOKIE = "eg_login"
LOGIN_SECONDS = 600
DISCOVERY_SECONDS = 3600
WRITES = {"POST", "PUT", "PATCH", "DELETE"}
MCP_PREFIXES = ("/mcp", "/sse", "/message")
# A probe makes the stack call an address the caller names.
OPERATOR_PREFIXES = (
    "/api/tokens",
    "/api/agents",
    "/api/summaries/probe",
    "/api/embeddings/probe",
)
ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA"]

ISSUER = os.environ.get("AUTH_OIDC_ISSUER", "").strip().rstrip("/")
# Where the stack itself reaches the issuer, when the browser's address for it
# does not resolve inside a container.
INTERNAL_ISSUER = os.environ.get("AUTH_OIDC_INTERNAL_URL", "").strip().rstrip("/")
PUBLIC_URL = os.environ.get("AUTH_PUBLIC_URL", "").strip().rstrip("/")
OPERATORS = {
    entry.strip().lower()
    for entry in os.environ.get("AUTH_OPERATORS", "").split(",")
    if entry.strip()
}

router = APIRouter()
_discovery: dict[str, Any] = {}


def read_secret(variable: str) -> str:
    """Read a value from the file an environment variable names, or ''."""
    path = os.environ.get(variable, "").strip()
    if path == "":
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        log.error("Cannot read %s (%s): %s", variable, path, error)
        return ""


def is_https(request: Request) -> bool:
    """Say whether the browser reached the gateway over https."""
    if PUBLIC_URL != "":
        return PUBLIC_URL.startswith("https://")
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def public_base(request: Request) -> str:
    """Return the address the browser reaches the gateway at."""
    if PUBLIC_URL != "":
        return PUBLIC_URL
    scheme = "https" if is_https(request) else "http"
    return f"{scheme}://{request.headers.get('host', 'localhost')}"


def local_target(value: str | None) -> str:
    """Return a path on this site to go back to; anything else is the root."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


def same_origin(request: Request) -> bool:
    """Say whether a form post came from this site, as the API guard does."""
    origin = request.headers.get("origin", "")
    if origin == "":
        return True
    host = request.headers.get("host", "").lower()
    return origin.lower() in (f"http://{host}", f"https://{host}")


def set_cookie(request: Request, answer: Response, name: str, value: str) -> None:
    """Set an HttpOnly cookie, Secure when the browser came over https."""
    answer.set_cookie(
        name,
        value,
        max_age=LOGIN_SECONDS if name == LOGIN_COOKIE else authn.SESSION_LIFE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )


# ---- the gateway's question --------------------------------------------------


def credential_of(request: Request) -> authn.Principal | None:
    """Return who a bearer token or a session cookie names, None for a bad one."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip().startswith(authn.TOKEN_PREFIX):
        return authn.verify_token(value.strip())
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie != "":
        return authn.read_session(cookie)
    return authn.ANONYMOUS


def queue_route(method: str, path: str) -> bool:
    """Say whether a request is one a summarization worker makes."""
    if path == "/worker/projects":
        return method == "GET"
    if path == "/worker/jobs":
        return True
    return path.startswith(("/worker/jobs/", "/worker/tasks/"))


def refusal(principal: authn.Principal, method: str, path: str) -> str | None:
    """Say why an authenticated principal may not make this request, or None."""
    if path.startswith(MCP_PREFIXES):
        # The MCP server weighs projects and write tools itself, per call.
        return None
    if path.startswith("/worker/"):
        # The worker API checks one shared token and no project at all.
        if authn.ALL_PROJECTS not in principal.projects:
            return "the worker API needs a token for every project"
        if method in WRITES and not principal.may("write-records"):
            return "this token may not write to the worker API"
        # The gateway hands the internal token on, so what it opens is weighed here.
        if not principal.operator and not queue_route(method, path):
            return "only an operator reaches the worker API past its job routes"
        return None
    if authn.ALL_PROJECTS not in principal.projects:
        return "the dashboard needs a token for every project"
    if path == "/api/ask" or path.startswith(OPERATOR_PREFIXES):
        return None if principal.operator else "only an operator may do this"
    if method in WRITES and not principal.may("admin"):
        return "a reader may not write"
    return None


def cross_origin_worker_write(request: Request, method: str, path: str) -> bool:
    """Say whether a browser session is writing to the worker API from elsewhere."""
    # A cookie rides along with any same-site page; a bearer token does not.
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    if bearer or not path.startswith("/worker/"):
        return False
    return method in WRITES and not same_origin(request)


def check_headers(
    principal: authn.Principal, request: Request, path: str
) -> dict[str, str]:
    """Return what the gateway passes upstream about the principal."""
    passed = ""
    if path.startswith("/worker/"):
        passed = request.headers.get("authorization", "")
        if principal.kind != "anonymous" and API_TOKEN != "":
            passed = f"Bearer {API_TOKEN}"
    return {
        "X-Auth-Kind": principal.kind,
        "X-Auth-Subject": principal.subject,
        "X-Auth-Role": principal.role,
        "X-Auth-Scopes": ",".join(principal.scopes),
        "X-Auth-Projects": ",".join(principal.projects),
        "X-Auth-Authorization": passed,
        "X-Auth-Host": principal.host or "",
    }


@router.get("/auth/check")
async def check(request: Request) -> Response:
    """Answer 204 with who the request is from, 401 for nobody, 403 for no."""
    method = request.headers.get("x-original-method", request.method).upper()
    path = urllib.parse.urlsplit(request.headers.get("x-original-uri", "/")).path
    mode = authn.enforce_mode()
    principal = await run_in_threadpool(credential_of, request)
    if principal is None or principal.kind == "anonymous":
        if mode == "on":
            return Response(status_code=401)
        if mode == "warn":
            what = "anonymous" if principal is not None else "bad credential"
            log.warning("%s request: %s %s", what, method, path)
        principal = authn.ANONYMOUS
    else:
        reason = refusal(principal, method, path)
        if reason is not None:
            log.info(
                "refused %s %s for %s: %s", method, path, principal.subject, reason
            )
            return Response(status_code=403, headers={"X-Auth-Reason": reason})
        if cross_origin_worker_write(request, method, path):
            log.info("refused %s %s: cross-origin with a session", method, path)
            reason = "a session writes to the worker API from this site only"
            return Response(status_code=403, headers={"X-Auth-Reason": reason})
    return Response(status_code=204, headers=check_headers(principal, request, path))


# ---- logging in ----------------------------------------------------------------


async def discovery() -> dict[str, Any]:
    """Return the issuer's OpenID configuration, read once an hour."""
    if _discovery.get("until", 0) > time.monotonic():
        return _discovery["document"]
    url = internal(f"{ISSUER}/.well-known/openid-configuration")
    async with httpx.AsyncClient(timeout=10) as client:
        answer = await client.get(url)
        answer.raise_for_status()
        document = answer.json()
    if document.get("issuer", "").rstrip("/") != ISSUER:
        raise RuntimeError(f"the issuer at {url} names itself {document.get('issuer')}")
    _discovery.update(document=document, until=time.monotonic() + DISCOVERY_SECONDS)
    return document


def internal(url: str) -> str:
    """Rewrite an address of the issuer to the one the stack reaches it at."""
    if INTERNAL_ISSUER != "" and url.startswith(ISSUER):
        return INTERNAL_ISSUER + url[len(ISSUER) :]
    return url


def oauth_client(request: Request) -> AsyncOAuth2Client:
    """Return the client the dashboard logs people in as."""
    return AsyncOAuth2Client(
        client_id=read_secret("AUTH_OIDC_CLIENT_ID_FILE"),
        client_secret=read_secret("AUTH_OIDC_CLIENT_SECRET_FILE") or None,
        scope="openid email profile",
        redirect_uri=f"{public_base(request)}/auth/callback",
        code_challenge_method="S256",
        timeout=10,
    )


def login_page(request: Request, status: int = 200, error: str = "") -> Response:
    """Render the login form, which takes an operator token in local mode."""
    return render(
        "login.html",
        status,
        section="",
        local=ISSUER == "",
        issuer=ISSUER,
        error=error,
        next=local_target(request.query_params.get("next")),
    )


@router.get("/auth/login")
async def login(request: Request) -> Response:
    """Send the browser to the issuer, or show the token form in local mode."""
    if ISSUER == "":
        return login_page(request)
    document = await discovery()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    target = local_target(request.query_params.get("next"))
    async with oauth_client(request) as client:
        uri, _ = client.create_authorization_url(
            document["authorization_endpoint"],
            state=state,
            code_verifier=verifier,
            nonce=nonce,
        )
    answer = RedirectResponse(uri, 302)
    pending = urllib.parse.urlencode(
        {"state": state, "verifier": verifier, "nonce": nonce, "next": target}
    )
    set_cookie(request, answer, LOGIN_COOKIE, pending)
    return answer


@router.post("/auth/login")
async def login_with_token(request: Request) -> Response:
    """Open a session for an operator token typed into the form."""
    if not same_origin(request):
        return login_page(request, 403, "Cross-origin login refused.")
    if ISSUER != "":
        return login_page(request, 400, "Log in through the identity provider.")
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    token = (form.get("token") or [""])[0].strip()
    target = local_target((form.get("next") or ["/"])[0])
    cookie = await run_in_threadpool(authn.login_with_token, token)
    if cookie is None:
        log.warning("refused a login token")
        return login_page(request, 401, "That is not an active operator token.")
    answer = RedirectResponse(target, 303)
    set_cookie(request, answer, SESSION_COOKIE, cookie)
    return answer


def claims_of(
    id_token: str, keys: dict[str, Any], nonce: str, issuer: str
) -> dict[str, Any]:
    """Verify an ID token against the issuer's keys and return its claims.

    `issuer` is the discovery document's own spelling, trailing slash and
    all, which is the one the issuer writes into `iss`.
    """
    token = jwt.decode(id_token, KeySet.import_key_set(keys), ID_TOKEN_ALGORITHMS)
    registry = jwt.JWTClaimsRegistry(
        leeway=60,
        iss={"essential": True, "value": issuer},
        aud={"essential": True, "value": read_secret("AUTH_OIDC_CLIENT_ID_FILE")},
        sub={"essential": True},
        exp={"essential": True},
        nonce={"essential": True, "value": nonce},
    )
    registry.validate(token.claims)
    return dict(token.claims)


def listed_operator(claims: dict[str, Any]) -> bool:
    """Say whether verified claims name someone AUTH_OPERATORS lists."""
    if str(claims["sub"]).lower() in OPERATORS:
        return True
    # An address the issuer did not verify is one anybody may have typed.
    if claims.get("email_verified") is not True:
        return False
    return str(claims.get("email") or "").lower() in OPERATORS - {""}


@router.get("/auth/callback")
async def callback(request: Request) -> Response:
    """Take the issuer's answer, check it and open a session."""
    pending = dict(urllib.parse.parse_qsl(request.cookies.get(LOGIN_COOKIE, "")))
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not pending or not hmac.compare_digest(pending.get("state", ""), state):
        return login_page(request, 400, "The login expired or was not started here.")
    if code == "":
        said = request.query_params.get("error_description") or "no code was returned"
        return login_page(request, 401, f"The identity provider refused: {said}")
    document = await discovery()
    try:
        async with oauth_client(request) as client:
            token = await client.fetch_token(
                internal(document["token_endpoint"]),
                code=code,
                code_verifier=pending.get("verifier", ""),
            )
        async with httpx.AsyncClient(timeout=10) as plain:
            keys = (await plain.get(internal(document["jwks_uri"]))).json()
        claims = claims_of(
            token.get("id_token", ""),
            keys,
            pending.get("nonce", ""),
            document["issuer"],
        )
    except (httpx.HTTPError, OAuthError, JoseError, ValueError, KeyError) as error:
        log.warning("OIDC callback refused: %s", error)
        return login_page(request, 401, "The identity provider's answer was refused.")
    subject = str(claims["sub"])
    email = str(claims.get("email") or "")
    operator = listed_operator(claims)
    display = email or str(claims.get("name") or subject)
    await run_in_threadpool(authn.record_login, ISSUER, subject, display, operator)
    cookie = await run_in_threadpool(authn.open_session, ISSUER, subject)
    answer = RedirectResponse(local_target(pending.get("next")), 303)
    set_cookie(request, answer, SESSION_COOKIE, cookie)
    answer.delete_cookie(LOGIN_COOKIE, path="/")
    return answer


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    """End the session this browser holds."""
    if not same_origin(request):
        return login_page(request, 403, "Cross-origin logout refused.")
    await run_in_threadpool(authn.end_session, request.cookies.get(SESSION_COOKIE, ""))
    answer = RedirectResponse("/auth/login", 303)
    answer.delete_cookie(SESSION_COOKIE, path="/")
    return answer
