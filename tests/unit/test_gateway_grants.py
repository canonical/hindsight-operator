# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""The grants store against a real PostgreSQL (002 FR-010, FR-011), and the
pure parts of member access in the policy."""

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))

from grants import SCHEMA, GrantsUnavailableError, PostgresGrants, resolve_database_url  # noqa: E402
from policy import (  # noqa: E402
    Decision,
    GrantsRoute,
    Kind,
    Member,
    PolicyError,
    check_member_access,
    classify,
    decide,
    grants_route,
    load_policy,
)


@pytest.fixture(scope="module")
def database_url():
    pg0 = pytest.importorskip("pg0")
    name = f"gateway-test-{uuid.uuid4().hex[:8]}"
    instance = pg0.Pg0(name=name)
    try:
        info = instance.start()
    except Exception as exc:  # no bundled PostgreSQL for this platform
        pytest.skip(f"pg0 cannot start PostgreSQL here: {exc}")
    yield info.uri
    instance.drop()


def _run(coroutine):
    return asyncio.run(coroutine)


def test_grants_are_shared_by_two_gateways_on_one_database(database_url):
    """FR-010, FR-011, SC-003: what one unit grants, another applies at once."""

    async def scenario():
        one, two = PostgresGrants(database_url), PostgresGrants(database_url)
        try:
            await one.grant("res-a", "ana@canonical.com")
            await one.grant("res-a", "ana@canonical.com")
            assert await two.banks_for("ana@canonical.com") == {"res-a"}
            assert await two.emails("res-a") == ["ana@canonical.com"]
            assert await two.withdraw("res-a", "ana@canonical.com") is True
            assert await one.banks_for("ana@canonical.com") == frozenset()
            assert await one.withdraw("res-a", "ana@canonical.com") is False
        finally:
            await one.close()
            await two.close()

    _run(scenario())


def test_grants_are_kept_in_their_own_schema(database_url):
    """FR-011."""
    import asyncpg

    async def scenario():
        store = PostgresGrants(database_url)
        await store.grant("res-b", "ben@canonical.com")
        await store.close()
        connection = await asyncpg.connect(database_url)
        try:
            tables = await connection.fetch(
                "SELECT table_schema, table_name FROM information_schema.tables"
                " WHERE table_name = 'bank_grants'"
            )
        finally:
            await connection.close()
        assert [(t["table_schema"], t["table_name"]) for t in tables] == [(SCHEMA, "bank_grants")]

    _run(scenario())


def test_an_unreachable_database_makes_grants_unavailable():
    async def scenario():
        store = PostgresGrants("postgresql://nobody:x@127.0.0.1:1/none")
        with pytest.raises(GrantsUnavailableError):
            await store.banks_for("ana@canonical.com")

    _run(scenario())


def test_an_external_database_url_is_used_as_it_is():
    url = "postgresql://hindsight:pw@db:5432/hindsight"
    assert resolve_database_url(url) == url


def test_an_embedded_database_that_is_not_running_is_unavailable():
    pytest.importorskip("pg0")
    with pytest.raises(GrantsUnavailableError):
        resolve_database_url(f"pg0://not-started-{uuid.uuid4().hex[:8]}")


def test_a_running_embedded_database_is_found_by_name(database_url):
    pg0 = pytest.importorskip("pg0")
    name = next(i.name for i in pg0.list_instances() if i.uri == database_url)
    assert resolve_database_url(f"pg0://{name}") == database_url


# ---- The policy's part --------------------------------------------------------


def test_a_member_has_write_access_to_exactly_its_granted_banks():
    member = Member("ana@canonical.com", frozenset({"res-a"}))
    write = classify("POST", "/v1/default/banks/res-a/memories")
    assert decide(member, write).allowed
    assert not decide(member, classify("POST", "/v1/default/banks/res-a2/memories")).allowed
    # Names match exactly: a granted name is never read as a pattern.
    starred = Member("ana@canonical.com", frozenset({"res-*"}))
    assert not decide(starred, classify("GET", "/v1/default/banks/res-a/profile")).allowed


def test_a_member_cannot_delete_a_bank_but_a_client_can():
    member = Member("ana@canonical.com", frozenset({"res-a"}))
    delete = classify("DELETE", "/v1/default/banks/res-a")
    assert delete.kind is Kind.BANK_WRITE and delete.deletes_bank
    assert decide(member, delete) == Decision(False, 403, "a member cannot delete a bank")
    assert not classify("DELETE", "/v1/default/banks/res-a/memories/m1").deletes_bank
    client = load_policy("admin", {"c": "k"}, '{"c": {"banks": ["res-*"]}}').clients[0]
    assert decide(client, delete).allowed


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("PUT", "/admin/banks/res-a", GrantsRoute("bank", "res-a")),
        ("GET", "/admin/banks/res-a/grants", GrantsRoute("grants", "res-a")),
        ("PUT", "/admin/banks/res-a/grants/A@B.com", GrantsRoute("grant", "res-a", "a@b.com")),
        (
            "DELETE",
            "/admin/banks/res-a/grants/a%40b.com",
            GrantsRoute("grant", "res-a", "a@b.com"),
        ),
        ("PUT", "/admin/banks/a%2Fb", Decision(False, 400, "invalid bank id")),
        ("PUT", "/admin/banks/res-a/../x", Decision(False, 400, "path is not normalised")),
        ("GET", "/admin/banks/res-a", Decision(False, 405, "method not allowed")),
        ("PUT", "/admin/banks/res-a/other/x", Decision(False, 404, "no such grants API route")),
    ],
)
def test_grants_routes(method, path, expected):
    assert grants_route(method, path) == expected


@pytest.mark.parametrize(
    ("issuer", "audience", "message"),
    [
        ("http://id.example.com", "hindsight", "https URL"),
        ("https://", "hindsight", "https URL"),
        ("https://id.example.com", "", "member-audience"),
    ],
)
def test_member_access_needs_an_https_issuer_and_an_audience(issuer, audience, message):
    """FR-013."""
    with pytest.raises(PolicyError, match=message):
        check_member_access(issuer, audience)


def test_the_grants_key_must_differ_from_every_other_key():
    """FR-013."""
    with pytest.raises(PolicyError, match="grants key"):
        load_policy("admin", {}, "", grants_key="admin")
    with pytest.raises(PolicyError, match="grants key"):
        load_policy("admin", {"c": "k"}, '{"c": {"banks": ["x"]}}', grants_key="k")
    assert load_policy("admin", {}, "", grants_key="g").identify("g") == "grants"
