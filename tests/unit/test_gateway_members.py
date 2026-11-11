# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Member bank access through the gateway (002): tokens from a fake issuer,
grants held in memory, and a fake Hindsight API behind it."""

import json
import logging
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))

from app import create_app  # noqa: E402
from grants import MemoryGrants  # noqa: E402
from policy import load_policy  # noqa: E402

ADMIN = "admin-key"
GRANTS_KEY = "grants-key"
KEYS = {"alice": "k-alice"}
ACCESS = {"alice": {"banks": ["alice-*"]}}
ISSUER = "https://id.example.com"
AUDIENCE = "hindsight"


def _streamed_json(status, body):
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(body).encode()),
    )


class FakeHindsight:
    def __init__(self):
        self.banks = {"res-a", "res-b", "other", "alice-1"}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        assert request.headers.get("authorization") in (None, f"Bearer {ADMIN}")
        path = request.url.path
        if path == "/v1/default/banks":
            banks = [{"bank_id": b} for b in sorted(self.banks)]
            return _streamed_json(200, {"banks": banks, "total": len(banks)})
        parts = path.split("/")
        if path.endswith("/profile") and request.method == "GET":
            found = parts[4] in self.banks
            return _streamed_json(200 if found else 404, {"bank_id": parts[4]})
        if len(parts) == 5 and request.method == "PUT":
            self.banks.add(parts[4])
            return _streamed_json(200, {"bank_id": parts[4]})
        return _streamed_json(200, {"path": request.url.raw_path.decode()})

    def reached(self, fragment: str) -> bool:
        return any(fragment in r.url.raw_path.decode() for r in self.seen)


class FakeIssuer:
    """An OpenID Connect issuer: discovery, keys, and tokens it signs."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.fetches = 0
        self.down = False

    def jwk(self):
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**data, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"jwks_uri": f"{ISSUER}/keys"})
        if request.url.path == "/keys":
            self.fetches += 1
            return httpx.Response(200, json={"keys": [self.jwk()]})
        return httpx.Response(404)

    def token(self, email="Ana@Canonical.com", **claims):
        body = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 300,
            "email": email,
            **claims,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def upstream():
    return FakeHindsight()


@pytest.fixture
def issuer():
    return FakeIssuer()


@pytest.fixture
def grants():
    store = MemoryGrants()
    store.rows = {"res-a": {"ana@canonical.com"}}
    return store


@pytest.fixture
def gateway(upstream, issuer, grants):
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS), grants_key=GRANTS_KEY)
    app = create_app(
        policy,
        "http://api.test",
        transport=httpx.MockTransport(upstream),
        member_issuer=ISSUER,
        member_audience=AUDIENCE,
        grants=grants,
        issuer_transport=httpx.MockTransport(issuer),
    )
    with TestClient(app) as client:
        yield client


def _as(key):
    return {"Authorization": f"Bearer {key}"}


# ---- Who a member is (FR-001, FR-002) -----------------------------------------


def test_a_member_reaches_a_granted_bank_by_their_token(gateway, upstream, issuer):
    """FR-001, FR-003: the email is compared without regard to case."""
    response = gateway.post("/v1/default/banks/res-a/memories/recall", headers=_as(issuer.token()))
    assert response.status_code == 200
    assert upstream.seen[-1].headers["authorization"] == f"Bearer {ADMIN}"


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 3600},
        {"iss": "https://other.example.com"},
        {"aud": "something-else"},
        {"email": None},
        {"email_verified": False},
    ],
    ids=["expired", "other-issuer", "other-audience", "no-email", "unverified"],
)
def test_a_token_that_does_not_qualify_is_unauthenticated(gateway, upstream, issuer, claims):
    """FR-001, FR-002."""
    response = gateway.get("/v1/default/banks/res-a/profile", headers=_as(issuer.token(**claims)))
    assert response.status_code == 401
    assert not upstream.reached("res-a")


def test_a_token_signed_with_another_key_is_unauthenticated(gateway, upstream, issuer):
    forger = FakeIssuer()
    response = gateway.get("/v1/default/banks/res-a/profile", headers=_as(forger.token()))
    assert response.status_code == 401


def test_a_symmetric_token_is_refused_whatever_its_secret(gateway, issuer):
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60, "email": "a@b.c"},
        "a-shared-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )
    assert gateway.get("/v1/default/banks/res-a/profile", headers=_as(token)).status_code == 401


def test_a_rotated_key_is_fetched_again(gateway, issuer):
    """Edge case: a key the issuer added after the last fetch is accepted."""
    assert gateway.get("/v1/default/banks/res-a/profile", headers=_as(issuer.token())).is_success
    issuer.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer.kid = "k2"
    verifier = gateway.app.state.verifier
    verifier._fetched_at -= 120  # past the refetch interval
    assert gateway.get("/v1/default/banks/res-a/profile", headers=_as(issuer.token())).is_success


def test_unknown_key_ids_do_not_refetch_more_than_once_a_minute(gateway, issuer):
    gateway.get("/v1/default/banks/res-a/profile", headers=_as(issuer.token()))
    before = issuer.fetches
    forger = FakeIssuer()
    forger.kid = "made-up"
    for _ in range(5):
        gateway.get("/v1/default/banks/res-a/profile", headers=_as(forger.token()))
    assert issuer.fetches == before


def test_an_unreachable_issuer_is_unavailable_for_members_only(gateway, issuer):
    """FR-015."""
    issuer.down = True
    response = gateway.get("/v1/default/banks/res-a/profile", headers=_as(issuer.token()))
    assert response.status_code == 503
    assert gateway.get("/v1/default/banks/alice-1/profile", headers=_as("k-alice")).is_success
    assert gateway.get("/v1/default/banks/other/profile", headers=_as(ADMIN)).is_success


# ---- What a member may do (FR-003 to FR-006) ----------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/default/banks/res-a/memories/list"),
        ("POST", "/v1/default/banks/res-a/memories"),
        ("POST", "/v1/default/banks/res-a/reflect"),
        ("PUT", "/v1/default/banks/res-a"),
        ("PATCH", "/v1/default/banks/res-a/config"),
        ("DELETE", "/v1/default/banks/res-a/memories/m1"),
    ],
)
def test_a_member_reads_and_writes_their_bank(gateway, issuer, method, path):
    """FR-003."""
    assert gateway.request(method, path, headers=_as(issuer.token())).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/default/banks/res-b/profile", 403),
        ("POST", "/v1/default/banks/res-b/memories/recall", 403),
        ("PUT", "/v1/default/banks/new-bank", 403),
        ("DELETE", "/v1/default/banks/res-a", 403),
        ("GET", "/metrics", 403),
        ("POST", "/mcp/", 403),
        ("GET", "/v1/default/banks/res-a%2Fx/profile", 400),
    ],
)
def test_a_member_is_refused_everything_else(gateway, upstream, issuer, method, path, status):
    """FR-003, FR-004, SC-001."""
    before = len(upstream.seen)
    response = gateway.request(method, path, headers=_as(issuer.token()))
    assert response.status_code == status
    assert len(upstream.seen) == before


def test_a_member_lists_only_their_banks(gateway, issuer):
    """FR-005, SC-002."""
    body = gateway.get("/v1/default/banks", headers=_as(issuer.token())).json()
    assert [b["bank_id"] for b in body["banks"]] == ["res-a"]
    assert body["total"] == 1


def test_a_member_without_grants_lists_nothing(gateway, issuer):
    token = issuer.token(email="nobody@canonical.com")
    assert gateway.get("/v1/default/banks", headers=_as(token)).json()["banks"] == []
    assert gateway.get("/v1/default/banks/res-a/profile", headers=_as(token)).status_code == 403


def _tool(name):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


def test_a_member_uses_read_and_write_tools_on_their_bank_only(gateway, issuer):
    """FR-006."""
    token = _as(issuer.token())
    assert gateway.post("/mcp/res-a/", json=_tool("recall"), headers=token).is_success
    assert gateway.post("/mcp/res-a/", json=_tool("retain"), headers=token).is_success
    assert gateway.post("/mcp/res-a/", json=_tool("list_banks"), headers=token).status_code == 403
    assert gateway.post("/mcp/res-b/", json=_tool("recall"), headers=token).status_code == 403


def test_a_refusal_is_logged_with_the_email_and_no_token(gateway, issuer, caplog):
    """FR-016."""
    token = issuer.token()
    with caplog.at_level(logging.INFO, logger="hindsight.gateway"):
        gateway.get("/v1/default/banks/res-b/profile", headers=_as(token))
    assert "denied ana@canonical.com GET /v1/default/banks/res-b/profile" in caplog.text
    assert token not in caplog.text


# ---- The grants API (FR-007 to FR-010) ----------------------------------------


def test_the_grants_api_creates_a_bank_once(gateway, upstream):
    """FR-007, FR-008."""
    assert gateway.put("/admin/banks/res-new", headers=_as(GRANTS_KEY)).status_code == 201
    assert "res-new" in upstream.banks
    assert gateway.put("/admin/banks/res-new", headers=_as(GRANTS_KEY)).status_code == 200


def test_grants_are_given_listed_and_withdrawn(gateway, issuer):
    """FR-007, FR-008, FR-010, SC-003."""
    key = _as(GRANTS_KEY)
    token = _as(issuer.token(email="ben@canonical.com"))
    assert gateway.get("/v1/default/banks/res-b/profile", headers=token).status_code == 403

    assert gateway.put("/admin/banks/res-b/grants/Ben@Canonical.com", headers=key).is_success
    assert gateway.put("/admin/banks/res-b/grants/ben@canonical.com", headers=key).is_success
    assert gateway.get("/admin/banks/res-b/grants", headers=key).json() == {
        "grants": ["ben@canonical.com"]
    }
    assert gateway.get("/v1/default/banks/res-b/profile", headers=token).is_success

    assert (
        gateway.delete("/admin/banks/res-b/grants/ben@canonical.com", headers=key).status_code
        == 204
    )
    assert (
        gateway.delete("/admin/banks/res-b/grants/ben@canonical.com", headers=key).status_code
        == 404
    )
    assert gateway.get("/v1/default/banks/res-b/profile", headers=token).status_code == 403


def test_an_email_with_a_plus_is_one_address(gateway):
    key = _as(GRANTS_KEY)
    assert gateway.put("/admin/banks/res-b/grants/ana+ml@canonical.com", headers=key).is_success
    assert gateway.get("/admin/banks/res-b/grants", headers=key).json() == {
        "grants": ["ana+ml@canonical.com"]
    }


def test_a_bank_that_does_not_exist_has_no_grants(gateway):
    key = _as(GRANTS_KEY)
    assert gateway.get("/admin/banks/missing/grants", headers=key).status_code == 404
    assert gateway.put("/admin/banks/missing/grants/a@b.com", headers=key).status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("PUT", "/admin/banks/res-a/grants/not-an-email", 400),
        ("PUT", "/admin/banks/res-a/grants/a@b@c", 400),
        ("PUT", "/admin/banks/res-a/grants/a%2F@b.com", 400),
        ("DELETE", "/admin/banks/res-a", 405),
        ("POST", "/admin/banks/res-a/grants", 405),
        ("GET", "/admin/other", 404),
    ],
)
def test_the_grants_api_refuses_what_it_does_not_offer(gateway, method, path, status):
    assert gateway.request(method, path, headers=_as(GRANTS_KEY)).status_code == status


def test_the_admin_key_may_use_the_grants_api(gateway):
    """FR-007."""
    assert gateway.get("/admin/banks/res-a/grants", headers=_as(ADMIN)).is_success


@pytest.mark.parametrize("who", ["k-alice", "member", None])
def test_nobody_else_may_use_the_grants_api(gateway, issuer, who):
    headers = _as(issuer.token() if who == "member" else who) if who else {}
    assert gateway.get("/admin/banks/res-a/grants", headers=headers).status_code in (401, 403)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/default/banks"),
        ("GET", "/v1/default/banks/res-a/profile"),
        ("POST", "/v1/default/banks/res-a/memories/recall"),
        ("GET", "/metrics"),
        ("POST", "/mcp/res-a/"),
    ],
)
def test_the_grants_key_reaches_nothing_but_the_grants_api(gateway, upstream, method, path):
    """FR-009, SC-004."""
    before = len(upstream.seen)
    assert gateway.request(method, path, headers=_as(GRANTS_KEY)).status_code == 403
    assert len(upstream.seen) == before


# ---- Turned off (FR-014) ------------------------------------------------------


def test_without_member_access_tokens_and_the_grants_key_are_refused(upstream, issuer):
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS))
    app = create_app(policy, "http://api.test", transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        token = _as(issuer.token())
        assert client.get("/v1/default/banks/res-a/profile", headers=token).status_code == 401
        assert client.get("/admin/banks/res-a/grants", headers=_as(GRANTS_KEY)).status_code == 401
