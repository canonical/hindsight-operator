# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""The bank-access gateway in front of a fake Hindsight API."""

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))

from app import create_app  # noqa: E402
from policy import load_policy  # noqa: E402

ADMIN = "admin-key"
KEYS = {"alice": "k-alice", "bob": "k-bob", "auditor": "k-auditor"}
ACCESS = {
    "alice": {"banks": ["alice-*"], "read-banks": ["shared"]},
    "bob": {"banks": ["bob-*"]},
    "auditor": {"read-banks": ["alice-*", "bob-*", "shared"]},
}
BANKS = ["alice-1", "alice-2", "bob-1", "shared", "secret"]


def _streamed_json(status: int, body: object, headers: dict[str, str] | None = None):
    """A response whose body streams, as the real API's does."""
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        stream=httpx.ByteStream(json.dumps(body).encode()),
    )


class FakeHindsight:
    """Records what reaches the API; answers like Hindsight would."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.headers.get("authorization") not in (None, f"Bearer {ADMIN}"):
            return _streamed_json(401, {"detail": "upstream saw a client key"})
        if request.url.path == "/v1/default/banks":
            banks = [{"bank_id": b, "name": b} for b in BANKS]
            return _streamed_json(200, {"banks": banks, "total": len(banks)})
        return _streamed_json(
            200,
            {"path": request.url.raw_path.decode(), "method": request.method},
            headers={"x-upstream": "yes"},
        )


@pytest.fixture
def upstream():
    return FakeHindsight()


@pytest.fixture
def gateway(upstream):
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS))
    app = create_app(policy, "http://api.test", transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        yield client


def _as(key):
    return {"Authorization": f"Bearer {key}"}


def test_public_routes_need_no_key_and_carry_none_upstream(gateway, upstream):
    """FR-001."""
    response = gateway.get("/health", headers={"X-Api-Key": "k-alice"})
    assert response.status_code == 200
    assert "authorization" not in upstream.seen[-1].headers
    assert "x-api-key" not in upstream.seen[-1].headers


def test_missing_or_wrong_key_is_401_and_never_forwarded(gateway, upstream):
    """FR-001, FR-016."""
    assert gateway.get("/v1/default/banks").status_code == 401
    assert gateway.get("/v1/default/banks", headers=_as("guess")).status_code == 401
    assert upstream.seen == []


def test_admin_sees_everything(gateway):
    """FR-002."""
    banks = gateway.get("/v1/default/banks", headers=_as(ADMIN)).json()["banks"]
    assert [b["bank_id"] for b in banks] == BANKS
    assert gateway.get("/metrics", headers=_as(ADMIN)).status_code == 200


def test_bank_list_is_filtered_per_key(gateway):
    """FR-003, SC-002."""

    def visible(key):
        data = gateway.get("/v1/default/banks", headers=_as(key)).json()
        return [b["bank_id"] for b in data["banks"]], data["total"]

    assert visible("k-alice") == (["alice-1", "alice-2", "shared"], 3)
    assert visible("k-bob") == (["bob-1"], 1)
    assert visible("k-auditor") == (["alice-1", "alice-2", "bob-1", "shared"], 4)


def test_bank_list_paging_applies_after_filtering(gateway):
    """FR-003."""
    data = gateway.get("/v1/default/banks?limit=1&offset=1", headers=_as("k-alice")).json()
    assert [b["bank_id"] for b in data["banks"]] == ["alice-2"]
    assert data["total"] == 3


def test_client_requests_go_upstream_with_the_admin_key(gateway, upstream):
    """FR-016."""
    response = gateway.post(
        "/v1/default/banks/alice-1/memories", headers=_as("k-alice"), json={"items": []}
    )
    assert response.status_code == 200
    assert response.headers["x-upstream"] == "yes"
    assert upstream.seen[-1].headers["authorization"] == f"Bearer {ADMIN}"


def test_isolation_between_tenants(gateway, upstream):
    """FR-004, FR-005, SC-001."""
    before = len(upstream.seen)
    for method, path in [
        ("GET", "/v1/default/banks/bob-1/memories"),
        ("POST", "/v1/default/banks/bob-1/memories/recall"),
        ("POST", "/v1/default/banks/bob-1/memories"),
        ("DELETE", "/v1/default/banks/bob-1"),
        ("POST", "/v1/default/banks/bob-1/document-transfer/export"),
    ]:
        response = gateway.request(method, path, headers=_as("k-alice"), json={})
        assert response.status_code == 403, (method, path)
    assert len(upstream.seen) == before  # nothing denied reached the API


def test_read_only_access(gateway):
    """FR-006, SC-003."""
    recall = gateway.post(
        "/v1/default/banks/bob-1/memories/recall", headers=_as("k-auditor"), json={}
    )
    assert recall.status_code == 200
    write = gateway.post("/v1/default/banks/bob-1/memories", headers=_as("k-auditor"), json={})
    assert write.status_code == 403
    assert "read-only" in write.json()["detail"]
    shared = gateway.post("/v1/default/banks/shared/memories", headers=_as("k-alice"), json={})
    assert shared.status_code == 403


def test_cross_bank_routes_are_admin_only(gateway):
    """FR-007."""
    for path in ("/v1/default/chunks/c1", "/v1/default/files/download/f", "/metrics"):
        assert gateway.get(path, headers=_as("k-alice")).status_code == 403
    assert gateway.post("/mcp/", headers=_as("k-alice"), json={}).status_code == 403


def test_traversal_never_reaches_the_api(gateway, upstream):
    """FR-009, SC-001."""
    # httpx normalises "/../" before sending, so the gateway sees "bob-1" (403);
    # a raw "/../" (curl --path-as-is) is a 400, see test_gateway_policy.py and
    # the spread tests. Either way, nothing reaches the API.
    before = len(upstream.seen)
    for path in (
        "/v1/default/banks/alice-1/../bob-1/memories/recall",
        "/v1/default/banks/alice-1%2F..%2Fbob-1/memories",
    ):
        response = gateway.post(path, headers=_as("k-alice"), json={})
        assert response.status_code in (400, 403), path
    assert len(upstream.seen) == before


def test_raw_path_is_forwarded_unchanged(gateway, upstream):
    """FR-009."""
    gateway.get("/v1/default/banks/alice-a%20b/memories", headers=_as("k-alice"))
    assert upstream.seen[-1].url.raw_path.decode() == "/v1/default/banks/alice-a%20b/memories"


def test_mcp_on_a_bank_is_admin_only_for_now(gateway, upstream):
    """FR-008."""
    before = len(upstream.seen)
    for key in ("k-alice", "k-auditor"):
        assert gateway.post("/mcp/alice-1/", headers=_as(key), json={}).status_code == 403
    assert len(upstream.seen) == before
    assert gateway.post("/mcp/alice-1/", headers=_as(ADMIN), json={}).is_success


def test_refusals_are_logged_without_the_key(gateway, caplog):
    """FR-017."""
    caplog.set_level(logging.INFO, logger="hindsight.gateway")
    gateway.delete("/v1/default/banks/bob-1", headers=_as("k-alice"))
    gateway.get("/v1/default/banks/bob-1/memories", headers=_as("k-wrong"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("denied")]
    reason = "this API key has no access to bank 'bob-1'"
    assert line == f"denied alice DELETE /v1/default/banks/bob-1: {reason}"
    assert not any(k in caplog.text for k in (*KEYS.values(), ADMIN, "k-wrong"))


def test_no_compression_the_client_did_not_ask_for(gateway, upstream):
    # curl sends no Accept-Encoding, so a compressed answer would reach it raw.
    del gateway.headers["accept-encoding"]
    gateway.get("/v1/default/banks/alice-1/memories", headers=_as("k-alice"))
    gateway.get("/health")
    for request in upstream.seen[-2:]:
        assert request.headers["accept-encoding"] == "identity"


def test_client_compression_preference_is_kept(gateway, upstream):
    gateway.get(
        "/v1/default/banks/alice-1/memories", headers={**_as("k-alice"), "Accept-Encoding": "gzip"}
    )
    assert upstream.seen[-1].headers["accept-encoding"] == "gzip"


def test_the_api_sees_the_clients_host_and_no_forwarding_headers(gateway, upstream):
    # The API builds absolute URLs, such as redirects, from the Host header.
    headers = {**_as("k-alice"), "Host": "hindsight.example", "X-Forwarded-For": "6.6.6.6"}
    gateway.get("/v1/default/banks/alice-1/memories", headers=headers)
    seen = upstream.seen[-1].headers
    assert seen["host"] == "hindsight.example"
    assert "x-forwarded-for" not in seen


def test_repeated_response_headers_are_kept(upstream):
    def two_cookies(request):
        upstream.seen.append(request)
        return httpx.Response(
            200,
            headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")],
            stream=httpx.ByteStream(b"{}"),
        )

    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS))
    app = create_app(policy, "http://api.test", transport=httpx.MockTransport(two_cookies))
    with TestClient(app) as client:
        response = client.get("/v1/default/banks/alice-1/memories", headers=_as("k-alice"))
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


@pytest.mark.parametrize("query", ["limit=abc", "offset=-1", "limit=1.5"])
def test_bank_list_rejects_invalid_paging(gateway, query):
    """FR-003."""
    response = gateway.get(f"/v1/default/banks?{query}", headers=_as("k-alice"))
    assert response.status_code == 422


def test_bank_list_limit_zero_lists_nothing(gateway):
    """FR-003."""
    body = gateway.get("/v1/default/banks?limit=0", headers=_as("k-alice")).json()
    assert body["banks"] == [] and body["total"] == 3
