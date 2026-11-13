# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""The bank-access gateway: per-key bank access in front of the Hindsight API.

The charm runs this with uvicorn on the API's public port, and moves the API to
a loopback-only port behind it. Clients present their own API key; the gateway
checks it against the policy (``policy.py``) and forwards allowed requests with
the admin key, which clients never see.

Configuration, from the environment the charm sets:

- ``HINDSIGHT_GATEWAY_UPSTREAM``: the API's loopback URL.
- ``HINDSIGHT_GATEWAY_ADMIN_KEY``: the admin (tenant) key, full access.
- ``HINDSIGHT_GATEWAY_KEYS``: JSON mapping client names to their keys.
- ``HINDSIGHT_GATEWAY_ACCESS``: JSON mapping client names to their banks.
- ``HINDSIGHT_GATEWAY_GRANTS_KEY``: the key that reaches only the grants API.
- ``HINDSIGHT_GATEWAY_MEMBER_ISSUER`` and ``HINDSIGHT_GATEWAY_MEMBER_AUDIENCE``:
  member access, on when both are set (002).
- ``HINDSIGHT_GATEWAY_DATABASE_URL``: the API's database URL, where grants are kept.

Runs inside the Hindsight image (Python 3.11), on its own Starlette and httpx.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, quote, urlencode

import httpx
from grants import GrantsUnavailableError, PostgresGrants
from identity import IdentityError, MemberVerifier, looks_like_token
from policy import (
    ADMIN_PREFIX,
    Client,
    GrantsRoute,
    Kind,
    Member,
    Policy,
    check_member_access,
    classify,
    decide,
    grants_route,
    load_policy,
    visible_bank,
)
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request as HttpRequest
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("hindsight.gateway")

# Headers that describe one hop, never forwarded.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
# Other ways a client might try to present credentials upstream, and the
# forwarding headers that the API trusts from loopback but ignored from the
# ingress before the gateway was in front of it.
STRIPPED_REQUEST = (HOP_BY_HOP - {"host"}) | {
    "authorization",
    "x-api-key",
    "cookie",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# The bank list is filtered here, so fetch it whole from the API.
_LIST_PAGE = 100_000


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _token(request: HttpRequest) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _forward_headers(request: HttpRequest, auth: str | None) -> list[tuple[str, str]]:
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in STRIPPED_REQUEST]
    if "accept-encoding" not in request.headers:
        # httpx would otherwise ask for gzip on the client's behalf, and the
        # compressed body would reach a client that cannot decode it.
        headers.append(("accept-encoding", "identity"))
    if auth:
        headers.append(("authorization", f"Bearer {auth}"))
    return headers


def _response_headers(response: httpx.Response) -> list[tuple[str, str]]:
    return [(k, v) for k, v in response.headers.multi_items() if k.lower() not in HOP_BY_HOP]


async def _body(response: httpx.Response):
    """The upstream body as it arrives, still encoded as the API sent it."""
    async for chunk in response.aiter_raw():
        yield chunk


def _streamed(response: httpx.Response) -> StreamingResponse:
    streamed = StreamingResponse(
        _body(response),
        status_code=response.status_code,
        background=BackgroundTask(response.aclose),
    )
    # Raw headers keep repeated ones, such as several Set-Cookie headers.
    streamed.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in _response_headers(response)
    ]
    return streamed


def _page_param(params: dict[str, str], name: str, default: int) -> int | None:
    """A non-negative integer query parameter, as the API accepts it; None if invalid."""
    value = params.pop(name, None)
    if value is None:
        return default
    return int(value) if value.isascii() and value.isdigit() else None


def create_app(
    policy: Policy,
    upstream: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    member_issuer: str = "",
    member_audience: str = "",
    grants=None,
    issuer_transport: httpx.AsyncBaseTransport | None = None,
):
    """Build the gateway app; the transports let tests stand in for the API and the issuer.

    Member access is on when ``member_issuer`` is set. ``grants`` then holds
    the grants (:mod:`grants`).
    """
    members_on = bool(member_issuer)

    @asynccontextmanager
    async def lifespan(app):
        async with (
            httpx.AsyncClient(
                base_url=upstream,
                transport=transport,
                timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            ) as client,
            # The issuer is outside the pod: reached through the model's proxy.
            httpx.AsyncClient(transport=issuer_transport, timeout=10.0, trust_env=True) as issuer,
        ):
            app.state.client = client
            app.state.verifier = (
                MemberVerifier(member_issuer, member_audience, issuer) if members_on else None
            )
            try:
                yield
            finally:
                if grants is not None:
                    await grants.close()

    async def forward(
        request: HttpRequest, path: str, auth: str | None, body: bytes | None = None
    ):
        client: httpx.AsyncClient = request.app.state.client
        # Forward the raw path the policy checked, so the API resolves the same bank.
        query = request.url.query
        upstream_request = client.build_request(
            request.method,
            path + (f"?{query}" if query else ""),
            headers=_forward_headers(request, auth),
            content=body if body is not None else request.stream(),
        )
        response = await client.send(upstream_request, stream=True)
        return _streamed(response)

    async def bank_exists(request: HttpRequest, quoted: str) -> bool | Response:
        client: httpx.AsyncClient = request.app.state.client
        response = await client.get(
            f"/v1/default/banks/{quoted}/profile",
            headers=[("authorization", f"Bearer {policy.admin_key}")],
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        return _error(502, f"Hindsight answered {response.status_code} for the bank")

    async def grants_api(request: HttpRequest, path: str) -> Response:
        route = grants_route(request.method, path)
        if not isinstance(route, GrantsRoute):
            return _error(route.status, route.reason)
        quoted = quote(route.bank, safe="")
        try:
            if route.action == "grant" and request.method == "DELETE":
                if await grants.withdraw(route.bank, route.email):
                    return Response(status_code=204)
                return _error(404, "no such grant")
            exists = await bank_exists(request, quoted)
            if isinstance(exists, Response):
                return exists
            if route.action == "bank":
                if exists:
                    return JSONResponse({"bank_id": route.bank})
                client: httpx.AsyncClient = request.app.state.client
                created = await client.put(
                    f"/v1/default/banks/{quoted}",
                    json={},
                    headers=[("authorization", f"Bearer {policy.admin_key}")],
                )
                if created.status_code >= 300:
                    return _error(
                        502, f"Hindsight refused to create the bank: {created.status_code}"
                    )
                return JSONResponse({"bank_id": route.bank}, status_code=201)
            if not exists:
                return _error(404, "no such bank")
            if route.action == "grants":
                return JSONResponse({"grants": await grants.emails(route.bank)})
            await grants.grant(route.bank, route.email)
            return JSONResponse({"bank_id": route.bank, "email": route.email})
        except GrantsUnavailableError:
            return _error(503, "grants are unavailable")

    async def member(request: HttpRequest, token: str) -> Member | Response:
        try:
            email = await request.app.state.verifier.email(token)
        except IdentityError as exc:
            if exc.status == 503:
                logger.warning("member token not checked: %s", exc.reason)
                return _error(503, exc.reason)
            return _error(401, f"Authentication failed: {exc.reason}")
        try:
            return Member(email, await grants.banks_for(email))
        except GrantsUnavailableError:
            return _error(503, "grants are unavailable")

    async def bank_list(request: HttpRequest, caller: Client | Member) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        params = dict(parse_qsl(request.url.query))
        limit = _page_param(params, "limit", 100)
        offset = _page_param(params, "offset", 0)
        if limit is None or offset is None:
            return _error(422, "limit and offset must be non-negative integers")
        params.update(limit=str(_LIST_PAGE), offset="0")
        response = await client.get(
            request.url.path + "?" + urlencode(params),
            headers=_forward_headers(request, policy.admin_key),
        )
        if response.status_code != 200:
            # httpx has decoded the body, so only its type still applies.
            return Response(
                response.content,
                response.status_code,
                media_type=response.headers.get("content-type"),
            )
        data = response.json()
        banks = [b for b in data.get("banks", []) if visible_bank(caller, str(b.get("bank_id")))]
        data.update(
            banks=banks[offset : offset + limit], total=len(banks), limit=limit, offset=offset
        )
        return JSONResponse(data)

    async def handle(request: HttpRequest) -> Response:
        path = request.scope.get("raw_path", b"").decode("latin-1").split("?", 1)[0]
        path = path or request.url.path
        req = classify(request.method, path)
        if req.kind is Kind.INVALID:
            return _error(400, req.reason)

        token = _token(request)
        caller = policy.identify(token)
        if req.kind is Kind.PUBLIC:
            return await forward(request, path, auth=None)
        if members_on and path.startswith(ADMIN_PREFIX):
            if caller in ("admin", "grants"):
                return await grants_api(request, path)
            return _error(401 if caller is None else 403, "only the grants key may use this API")
        if caller == "grants":
            return _error(403, "the grants key can only use the grants API")
        if caller is None and members_on and token and looks_like_token(token):
            caller = await member(request, token)
            if isinstance(caller, Response):
                return caller
        if caller is None:
            return _error(401, "Authentication failed: Invalid API key")
        if caller == "admin":
            return await forward(request, path, auth=policy.admin_key)

        body = None
        if req.kind is Kind.MCP_BANK and request.method == "POST":
            body = await request.body()
        decision = decide(caller, req, body)
        if not decision.allowed:
            logger.info("denied %s %s %s: %s", caller.name, request.method, path, decision.reason)
            return _error(decision.status, decision.reason)
        if req.kind is Kind.BANK_LIST:
            return await bank_list(request, caller)
        return await forward(request, path, auth=policy.admin_key, body=body)

    routes = [Route("/{path:path}", handle, methods=METHODS)]
    return Starlette(routes=routes, lifespan=lifespan)


def _from_environment():
    issuer = os.environ.get("HINDSIGHT_GATEWAY_MEMBER_ISSUER", "")
    audience = os.environ.get("HINDSIGHT_GATEWAY_MEMBER_AUDIENCE", "")
    if issuer:
        check_member_access(issuer, audience)
    policy = load_policy(
        os.environ["HINDSIGHT_GATEWAY_ADMIN_KEY"],
        json.loads(os.environ.get("HINDSIGHT_GATEWAY_KEYS") or "{}"),
        os.environ.get("HINDSIGHT_GATEWAY_ACCESS") or "{}",
        grants_key=os.environ.get("HINDSIGHT_GATEWAY_GRANTS_KEY", "") if issuer else "",
    )
    logging.basicConfig(level=logging.INFO)
    logger.info(
        "bank-access gateway: %d client key(s), member access %s",
        len(policy.clients),
        "on" if issuer else "off",
    )
    grants = (
        PostgresGrants(os.environ.get("HINDSIGHT_GATEWAY_DATABASE_URL", "")) if issuer else None
    )
    return create_app(
        policy,
        os.environ["HINDSIGHT_GATEWAY_UPSTREAM"],
        member_issuer=issuer,
        member_audience=audience,
        grants=grants,
    )


def app_factory():
    """Entry point for ``uvicorn --factory app:app_factory``."""
    return _from_environment()
