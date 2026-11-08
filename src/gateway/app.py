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

Runs inside the Hindsight image (Python 3.11), on its own Starlette and httpx.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode

import httpx
from policy import Client, Kind, Policy, classify, decide, load_policy, visible_bank
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
# Other ways a client might try to present credentials upstream.
STRIPPED_REQUEST = HOP_BY_HOP | {"authorization", "x-api-key", "cookie"}
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
    """The upstream body as it arrives, or whole if httpx has already read it."""
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


def create_app(policy: Policy, upstream: str, transport: httpx.AsyncBaseTransport | None = None):
    """Build the gateway app; ``transport`` lets tests stand in for the API."""

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            base_url=upstream,
            transport=transport,
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        ) as client:
            app.state.client = client
            yield

    async def forward(request: HttpRequest, path: str, auth: str | None):
        client: httpx.AsyncClient = request.app.state.client
        # Forward the raw path the policy checked, so the API resolves the same bank.
        query = request.url.query
        upstream_request = client.build_request(
            request.method,
            path + (f"?{query}" if query else ""),
            headers=_forward_headers(request, auth),
            content=request.stream(),
        )
        response = await client.send(upstream_request, stream=True)
        return StreamingResponse(
            _body(response),
            status_code=response.status_code,
            headers=dict(_response_headers(response)),
            background=BackgroundTask(response.aclose),
        )

    async def bank_list(request: HttpRequest, caller: Client) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        params = dict(parse_qsl(request.url.query))
        limit = int(params.pop("limit", 100) or 100)
        offset = int(params.pop("offset", 0) or 0)
        params.update(limit=str(_LIST_PAGE), offset="0")
        response = await client.get(
            request.url.path + "?" + urlencode(params),
            headers=_forward_headers(request, policy.admin_key),
        )
        if response.status_code != 200:
            return Response(
                response.content, response.status_code, dict(_response_headers(response))
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

        caller = policy.identify(_token(request))
        if req.kind is Kind.PUBLIC:
            return await forward(request, path, auth=None)
        if caller is None:
            return _error(401, "Authentication failed: Invalid API key")
        if caller == "admin":
            return await forward(request, path, auth=policy.admin_key)

        decision = decide(caller, req)
        if not decision.allowed:
            logger.info("denied %s %s %s: %s", caller.name, request.method, path, decision.reason)
            return _error(decision.status, decision.reason)
        if req.kind is Kind.BANK_LIST:
            return await bank_list(request, caller)
        return await forward(request, path, auth=policy.admin_key)

    routes = [Route("/{path:path}", handle, methods=METHODS)]
    return Starlette(routes=routes, lifespan=lifespan)


def _from_environment():
    policy = load_policy(
        os.environ["HINDSIGHT_GATEWAY_ADMIN_KEY"],
        json.loads(os.environ.get("HINDSIGHT_GATEWAY_KEYS") or "{}"),
        os.environ.get("HINDSIGHT_GATEWAY_ACCESS") or "{}",
    )
    logging.basicConfig(level=logging.INFO)
    logger.info("bank-access gateway: %d client key(s)", len(policy.clients))
    return create_app(policy, os.environ["HINDSIGHT_GATEWAY_UPSTREAM"])


def app_factory():
    """Entry point for ``uvicorn --factory app:app_factory``."""
    return _from_environment()
