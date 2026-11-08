# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Per-key bank access (``bank-access``): the charm and its gateway.

Each request goes through the gateway on the API's port. These tests check that
it is in front of the API, that each key only sees and uses its own banks, and
that turning the feature off restores direct access.
"""

from __future__ import annotations

import jubilant
import pytest

from conftest import APP
from helpers import api_environment, http, wait_active

UNIT = f"{APP}/0"
ADMIN = "it-admin-key"
KEYS = {"alice": "it-key-alice", "bob": "it-key-bob", "auditor": "it-key-auditor"}
BANK_ACCESS = """
alice:
  banks: [alice-*]
  read-banks: [shared]
bob:
  banks: [bob-*]
auditor:
  read-banks: [alice-*, bob-*, shared]
"""
BANKS = ["alice-notes", "bob-notes", "shared", "private"]


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    env = juju.add_secret("hindsight-env", {"tenant-api-key": ADMIN})
    keys = juju.add_secret("hindsight-access-keys", KEYS)
    juju.deploy(charm, APP, resources=resources, storage={"data": "1G"})
    for secret in (env, keys):
        juju.grant_secret(secret, APP)
    juju.config(APP, {"env-secret": env, "access-keys": keys, "bank-access": BANK_ACCESS})
    wait_active(juju)
    for bank in BANKS:
        assert http(juju, UNIT, "PUT", f"/v1/default/banks/{bank}", {}, token=ADMIN).status == 200


def _banks(juju, key):
    response = http(juju, UNIT, "GET", "/v1/default/banks", token=key)
    assert response.status == 200, response.body
    return sorted(b["bank_id"] for b in response.json()["banks"])


def test_api_listens_only_behind_the_gateway(juju: jubilant.Juju):
    env = api_environment(juju, UNIT)
    assert (env["HINDSIGHT_API_HOST"], env["HINDSIGHT_API_PORT"]) == ("127.0.0.1", "8890")
    assert http(juju, UNIT, "GET", "/health").status == 200


def test_unknown_or_missing_key_is_refused(juju: jubilant.Juju):
    assert http(juju, UNIT, "GET", "/v1/default/banks").status == 401
    assert http(juju, UNIT, "GET", "/v1/default/banks", token="guess").status == 401


def test_admin_sees_every_bank(juju: jubilant.Juju):
    assert _banks(juju, ADMIN) == sorted(BANKS)


def test_each_key_sees_only_its_banks(juju: jubilant.Juju):
    assert _banks(juju, KEYS["alice"]) == ["alice-notes", "shared"]
    assert _banks(juju, KEYS["bob"]) == ["bob-notes"]
    assert _banks(juju, KEYS["auditor"]) == ["alice-notes", "bob-notes", "shared"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/default/banks/bob-notes/memories/recall"),
        ("POST", "/v1/default/banks/bob-notes/memories"),
        ("GET", "/v1/default/banks/bob-notes/documents"),
        ("POST", "/v1/default/banks/bob-notes/document-transfer/export"),
        ("DELETE", "/v1/default/banks/bob-notes"),
        ("GET", "/v1/default/banks/private/memories"),
    ],
)
def test_other_tenants_banks_are_refused(juju: jubilant.Juju, method, path):
    response = http(juju, UNIT, method, path, {"query": "x"}, token=KEYS["alice"])
    assert response.status == 403, response.body


def test_read_only_access(juju: jubilant.Juju):
    recall = "/v1/default/banks/shared/memories/recall"
    assert http(juju, UNIT, "POST", recall, {"query": "x"}, token=KEYS["alice"]).status == 200
    write = http(
        juju,
        UNIT,
        "POST",
        "/v1/default/banks/shared/memories",
        {"items": [{"content": "x"}], "async": False},
        token=KEYS["alice"],
    )
    assert write.status == 403 and "read-only" in write.body
    create = http(juju, UNIT, "PUT", "/v1/default/banks/auditor-own", {}, token=KEYS["auditor"])
    assert create.status == 403


def test_keys_create_banks_only_within_their_patterns(juju: jubilant.Juju):
    assert (
        http(juju, UNIT, "PUT", "/v1/default/banks/alice-new", {}, token=KEYS["alice"]).status
        == 200
    )
    assert (
        http(juju, UNIT, "PUT", "/v1/default/banks/carol-new", {}, token=KEYS["alice"]).status
        == 403
    )
    assert "alice-new" in _banks(juju, KEYS["alice"])
    assert "alice-new" not in _banks(juju, KEYS["bob"])


@pytest.mark.parametrize(
    "path", ["/metrics", "/v1/default/chunks/some-chunk", "/v1/default/files/download/x"]
)
def test_cross_bank_routes_are_admin_only(juju: jubilant.Juju, path):
    assert http(juju, UNIT, "GET", path, token=KEYS["alice"]).status == 403


def test_raw_path_traversal_is_rejected(juju: jubilant.Juju):
    # urllib sends the path as written, like curl --path-as-is.
    path = "/v1/default/banks/alice-notes/../bob-notes/memories/recall"
    assert http(juju, UNIT, "POST", path, {"query": "x"}, token=KEYS["alice"]).status == 400


def _initialize():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "it", "version": "0"},
        },
    }


def test_mcp_per_bank(juju: jubilant.Juju):
    def mcp(bank, body, key):
        return http(juju, UNIT, "POST", f"/mcp/{bank}/", body, token=key, accept_sse=True)

    assert mcp("alice-notes", _initialize(), KEYS["alice"]).status == 200
    assert mcp("bob-notes", _initialize(), KEYS["alice"]).status == 403
    retain = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "retain"}}
    assert mcp("bob-notes", retain, KEYS["auditor"]).status == 403
    assert http(juju, UNIT, "POST", "/mcp/", _initialize(), token=KEYS["alice"]).status == 403


def test_control_plane_still_uses_the_admin_view(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/", port=9999)
    assert response.status < 500, response.body


def test_turning_bank_access_off_restores_direct_access(juju: jubilant.Juju):
    juju.config(APP, reset=["bank-access"])
    wait_active(juju)
    env = api_environment(juju, UNIT)
    assert (env["HINDSIGHT_API_HOST"], env["HINDSIGHT_API_PORT"]) == ("0.0.0.0", "8888")
    assert http(juju, UNIT, "GET", "/v1/default/banks", token=KEYS["alice"]).status == 401
    assert http(juju, UNIT, "GET", "/v1/default/banks", token=ADMIN).status == 200
