# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""The bank-access gateway's policy: classification and decisions."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))

from policy import (  # noqa: E402
    Client,
    Kind,
    PolicyError,
    classify,
    decide,
    load_policy,
    mcp_tools_called,
    visible_bank,
)

ALICE = Client("alice", "k-alice", banks=("alice-*",), read_banks=("shared",))
AUDITOR = Client("auditor", "k-auditor", read_banks=("alice-*", "bob-*"))


@pytest.mark.parametrize(
    "method,path,kind,bank",
    [
        ("GET", "/health", Kind.PUBLIC, None),
        ("GET", "/docs", Kind.PUBLIC, None),
        ("GET", "/openapi.json", Kind.PUBLIC, None),
        ("POST", "/health", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/default/banks", Kind.BANK_LIST, None),
        ("GET", "/v1/default/banks/alice-1/memories", Kind.BANK_READ, "alice-1"),
        ("GET", "/v1/default/banks/alice-1", Kind.BANK_READ, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/memories/recall", Kind.BANK_READ, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/reflect", Kind.BANK_READ, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/document-transfer/export", Kind.BANK_READ, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/memories", Kind.BANK_WRITE, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/import", Kind.BANK_WRITE, "alice-1"),
        ("POST", "/v1/default/banks/alice-1/document-transfer", Kind.BANK_WRITE, "alice-1"),
        ("PUT", "/v1/default/banks/alice-1", Kind.BANK_WRITE, "alice-1"),
        ("PATCH", "/v1/default/banks/alice-1/config", Kind.BANK_WRITE, "alice-1"),
        ("DELETE", "/v1/default/banks/alice-1", Kind.BANK_WRITE, "alice-1"),
        ("OPTIONS", "/v1/default/banks/alice-1", Kind.ADMIN_ONLY, None),
        ("POST", "/mcp/alice-1/", Kind.MCP_BANK, "alice-1"),
        ("GET", "/mcp/alice-1", Kind.MCP_BANK, "alice-1"),
        ("POST", "/mcp/", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/default/chunks/abc", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/default/files/download/x", Kind.ADMIN_ONLY, None),
        ("GET", "/metrics", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/bank-template-schema", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/some-future-route", Kind.ADMIN_ONLY, None),
        ("GET", "/v1/default/banks/a%20b/memories", Kind.BANK_READ, "a b"),
    ],
)
def test_classify(method, path, kind, bank):
    """FR-007, FR-008."""
    request = classify(method, path)
    assert request.kind is kind
    assert request.bank == bank


@pytest.mark.parametrize(
    "path",
    [
        "/v1/default/banks/alice-1/../bob-1/memories/recall",
        "/v1/default/banks/alice-1/./x",
        "/v1/default/banks//x",
        "/v1/default/banks/bob%2F1/memories",
        "/v1/default/banks/bob%2f1/memories",
        "/v1/default/banks/%2E%2E/memories",
        "/v1/default/banks/a%00b/memories",
        "/mcp/%2E%2E/",
        "/mcp/a%2Fb/",
    ],
)
def test_path_tricks_are_invalid(path):
    """FR-009."""
    assert classify("GET", path).kind is Kind.INVALID


def test_read_write_and_none():
    """FR-004, FR-005."""
    read = classify("POST", "/v1/default/banks/alice-1/memories/recall")
    write = classify("POST", "/v1/default/banks/alice-1/memories")
    assert decide(ALICE, read).allowed and decide(ALICE, write).allowed

    shared_write = decide(ALICE, classify("POST", "/v1/default/banks/shared/memories"))
    assert not shared_write.allowed and "read-only" in shared_write.reason
    assert decide(ALICE, classify("GET", "/v1/default/banks/shared/memories")).allowed

    other = decide(ALICE, classify("GET", "/v1/default/banks/bob-1/memories"))
    assert (other.allowed, other.status) == (False, 403)


def test_read_only_key_cannot_write_or_create():
    """FR-005, FR-006."""
    assert decide(AUDITOR, classify("POST", "/v1/default/banks/bob-1/memories/recall")).allowed
    for method, path in [
        ("POST", "/v1/default/banks/bob-1/memories"),
        ("PUT", "/v1/default/banks/bob-1"),
        ("DELETE", "/v1/default/banks/bob-1"),
        ("PUT", "/v1/default/banks/new-bank"),
    ]:
        assert not decide(AUDITOR, classify(method, path)).allowed


def test_pattern_matching_is_exact_and_case_sensitive():
    """FR-004, FR-005."""
    assert not ALICE.can_read("Alice-1")
    assert not ALICE.can_read("xalice-1")
    assert not ALICE.can_read("shared-2")
    assert ALICE.can_write("alice-")


def test_admin_only_and_invalid_routes_are_refused():
    """FR-007, FR-008, FR-009."""
    assert decide(ALICE, classify("GET", "/metrics")).status == 403
    assert decide(ALICE, classify("POST", "/mcp/")).status == 403
    assert decide(ALICE, classify("GET", "/v1/default/banks/a/../b")).status == 400


def _call(tool):
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}}
    ).encode()


def test_mcp_tools_by_key():
    """FR-010."""
    req = classify("POST", "/mcp/alice-1/")
    assert decide(ALICE, req, _call("retain")).allowed
    assert decide(ALICE, req, _call("recall")).allowed
    assert decide(ALICE, req, b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}').allowed

    read_only = classify("POST", "/mcp/bob-1/")
    assert decide(AUDITOR, read_only, _call("recall")).allowed
    assert not decide(AUDITOR, read_only, _call("retain")).allowed
    assert not decide(AUDITOR, read_only, _call("sync_retain")).allowed
    assert not decide(ALICE, req, _call("list_banks")).allowed
    assert not decide(ALICE, req, _call("create_bank")).allowed
    assert not decide(ALICE, classify("POST", "/mcp/bob-1/"), _call("recall")).allowed


def test_mcp_batches_and_garbage():
    """FR-010."""
    batch = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "recall"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "retain"}},
        ]
    ).encode()
    assert mcp_tools_called(batch) == ["recall", "retain"]
    assert not decide(AUDITOR, classify("POST", "/mcp/bob-1/"), batch).allowed
    for body in (b"not json", b"[1]", b'{"method":"tools/call","params":{}}'):
        assert decide(ALICE, classify("POST", "/mcp/alice-1/"), body).status == 400


def test_bank_visibility():
    """FR-003."""
    assert visible_bank("admin", "anything")
    assert visible_bank(ALICE, "alice-9") and visible_bank(ALICE, "shared")
    assert not visible_bank(ALICE, "bob-1")


def test_identify_admin_and_clients():
    """FR-001."""
    policy = load_policy(
        "admin-key",
        {"alice": "k-alice", "auditor": "k-auditor"},
        json.dumps({"alice": {"banks": ["alice-*"]}, "auditor": {"read-banks": ["*"]}}),
    )
    assert policy.identify("admin-key") == "admin"
    assert policy.identify("k-alice").name == "alice"
    assert policy.identify("k-auditor").read_banks == ("*",)
    assert policy.identify("nope") is None
    assert policy.identify("") is None and policy.identify(None) is None


@pytest.mark.parametrize(
    "keys,access,error",
    [
        ({}, {"alice": {"banks": ["a"]}}, "no key"),
        ({"alice": "admin-key"}, {"alice": {"banks": ["a"]}}, "admin key"),
        ({"a": "same", "b": "same"}, {"a": {"banks": ["x"]}, "b": {"banks": ["y"]}}, "another"),
        ({"alice": "k"}, {"alice": {"writes": ["a"]}}, "only 'banks'"),
        ({"alice": "k"}, {"alice": {"banks": "a"}}, "list"),
        ({"Alice": "k"}, {"Alice": {"banks": ["a"]}}, "invalid client name"),
        ({"alice": "k"}, ["alice"], "must map"),
    ],
)
def test_invalid_policies(keys, access, error):
    """FR-013."""
    with pytest.raises(PolicyError, match=error):
        load_policy("admin-key", keys, json.dumps(access))


def test_admin_key_required():
    """FR-013."""
    with pytest.raises(PolicyError, match="admin key"):
        load_policy("", {}, "{}")
