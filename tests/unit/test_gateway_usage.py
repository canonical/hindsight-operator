# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Usage metrics (003): request counts read from the metrics port, and the LLM
usage totals swept from a real ``llm_requests`` table in PostgreSQL."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app  # noqa: E402
from grants import MemoryGrants  # noqa: E402
from llm_usage import LlmUsage, Usage, cost, lines  # noqa: E402
from policy import Client, Member, classify, load_policy  # noqa: E402
from test_gateway_members import ADMIN, AUDIENCE, GRANTS_KEY, ISSUER, FakeIssuer  # noqa: E402
from usage import caller_label, operation  # noqa: E402

KEYS = {"alice": "k-alice"}
ACCESS = {"alice": {"banks": ["alice-*"], "read-banks": ["shared"]}}
HINDSIGHT_METRICS = (
    '# TYPE hindsight_operation_total counter\nhindsight_operation_total{operation="retain"} 3\n'
)
PRICES = {
    "gpt-4o": {
        "input_cost_per_token": 2.5e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06,
    },
    "gemini/gemini-2.5-flash": {"input_cost_per_token": 3e-07, "output_cost_per_token": 2.5e-06},
}


def _streamed(status: int, body: bytes, content_type="application/json") -> httpx.Response:
    """A response whose body streams, as the real API's does."""
    return httpx.Response(
        status, headers={"content-type": content_type}, stream=httpx.ByteStream(body)
    )


class FakeHindsight:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") in (None, f"Bearer {ADMIN}")
        if request.url.path == "/metrics":
            return _streamed(200, HINDSIGHT_METRICS.encode(), "text/plain")
        if request.url.path == "/v1/default/banks":
            banks = {"banks": [{"bank_id": "alice-1"}], "total": 1}
            return _streamed(200, json.dumps(banks).encode())
        if request.url.path.endswith("/boom"):
            return _streamed(500, b'{"detail": "boom"}')
        return _streamed(200, b'{"ok": true}')


def _as(key):
    return {"Authorization": f"Bearer {key}"}


def _tool(name):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


@pytest.fixture
def issuer():
    return FakeIssuer()


@pytest.fixture
def gateway(issuer):
    grants = MemoryGrants()
    grants.rows = {"res-a": {"ana@canonical.com"}}
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS), grants_key=GRANTS_KEY)
    app = create_app(
        policy,
        "http://api.test",
        transport=httpx.MockTransport(FakeHindsight()),
        member_issuer=ISSUER,
        member_audience=AUDIENCE,
        grants=grants,
        issuer_transport=httpx.MockTransport(issuer),
        metrics_port=0,
    )
    with TestClient(app) as client:
        yield client


def _scrape(gateway, path="/metrics") -> httpx.Response:
    port = gateway.app.state.metrics_server.sockets[0].getsockname()[1]
    return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=5)


def _counts(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        if line.startswith("hindsight_gateway_requests_total{"):
            series, _, value = line.rpartition(" ")
            out[series.removeprefix("hindsight_gateway_requests_total")] = int(value)
    return out


def _series(client, bank, op, code):
    return f'{{client="{client}",bank="{bank}",operation="{op}",code="{code}"}}'


# ---- Request counts (FR-001 to FR-004) ----------------------------------------


def test_each_caller_is_counted_by_bank_operation_and_status(gateway, issuer):
    """FR-001, FR-002, FR-003, SC-001."""
    bank = "/v1/default/banks"
    gateway.post(f"{bank}/alice-1/memories", headers=_as("k-alice"), json={})
    gateway.post(f"{bank}/alice-1/memories", headers=_as("k-alice"), json={})
    gateway.post(f"{bank}/alice-1/memories/recall", headers=_as("k-alice"), json={})
    gateway.post(f"{bank}/shared/memories", headers=_as("k-alice"), json={})
    gateway.post(f"{bank}/secret/reflect", headers=_as("k-alice"), json={})
    gateway.get(f"{bank}/alice-1/boom", headers=_as("k-alice"))
    gateway.get(bank, headers=_as("k-alice"))
    gateway.post(f"{bank}/res-a/reflect", headers=_as(issuer.token()), json={})
    gateway.delete(f"{bank}/any-bank", headers=_as(ADMIN))
    gateway.put("/admin/banks/res-z", headers=_as(GRANTS_KEY))
    gateway.get(f"{bank}/res-a/profile", headers=_as(GRANTS_KEY))
    gateway.get(f"{bank}/made-up-1/profile", headers=_as("wrong"))
    gateway.get(f"{bank}/a%2Fb/profile", headers=_as("k-alice"))
    gateway.get("/health")

    assert _counts(_scrape(gateway).text) == {
        _series("alice", "alice-1", "retain", "2xx"): 2,
        _series("alice", "alice-1", "recall", "2xx"): 1,
        _series("alice", "alice-1", "read", "5xx"): 1,
        _series("alice", "", "list-banks", "2xx"): 1,
        # Refused: read-only bank, then a bank alice has no access to.
        _series("alice", "", "retain", "4xx"): 1,
        _series("alice", "", "reflect", "4xx"): 1,
        _series("member", "res-a", "reflect", "2xx"): 1,
        _series("admin", "any-bank", "delete-bank", "2xx"): 1,
        _series("grants", "", "grants", "2xx"): 1,
        _series("grants", "", "read", "4xx"): 1,
        # Neither a wrong key nor an invalid path adds its bank name.
        _series("unauthenticated", "", "read", "4xx"): 1,
        _series("unauthenticated", "", "invalid", "4xx"): 1,
    }


def test_no_email_key_or_token_reaches_the_metrics(gateway, issuer):
    """FR-002."""
    token = issuer.token()
    gateway.post("/v1/default/banks/res-a/memories", headers=_as(token), json={})
    gateway.get("/v1/default/banks/x/profile", headers=_as("k-alice"))
    text = _scrape(gateway).text
    for secret in ("canonical.com", "Canonical.com", token, "k-alice", ADMIN, GRANTS_KEY):
        assert secret not in text


def test_mcp_calls_are_counted_by_the_tool_they_call(gateway):
    """FR-004."""
    mcp = "/mcp/alice-1/"
    gateway.post(mcp, headers=_as("k-alice"), json=_tool("recall"))
    gateway.post(mcp, headers=_as("k-alice"), json=_tool("sync_retain"))
    gateway.post(mcp, headers=_as("k-alice"), json=_tool("list_mental_models"))
    gateway.post(mcp, headers=_as("k-alice"), json=[_tool("recall"), _tool("retain")])
    counts = _counts(_scrape(gateway).text)
    assert counts[_series("alice", "alice-1", "recall", "2xx")] == 1
    assert counts[_series("alice", "alice-1", "retain", "2xx")] == 1
    assert counts[_series("alice", "alice-1", "mcp", "2xx")] == 2


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/v1/default/banks/b/memories", "retain"),
        ("POST", "/v1/default/banks/b/memories/", "retain"),
        ("POST", "/v1/default/banks/b/memories/recall", "recall"),
        ("POST", "/v1/default/banks/b/reflect", "reflect"),
        ("GET", "/v1/default/banks/b/memories/list", "read"),
        ("PATCH", "/v1/default/banks/b/config", "write"),
        ("DELETE", "/v1/default/banks/b", "delete-bank"),
        ("DELETE", "/v1/default/banks/b/memories", "write"),
        ("GET", "/v1/default/banks", "list-banks"),
        ("GET", "/mcp/b/", "mcp"),
        ("GET", "/metrics", "admin"),
        ("GET", "/v1/default/banks/../x", "invalid"),
    ],
)
def test_operations(method, path, expected):
    """FR-004."""
    assert operation(method, path, classify(method, path)) == expected


def test_callers_are_named_without_secrets():
    """FR-002."""
    client = Client("medic", "secret-key")
    assert caller_label(client) == "medic"
    assert caller_label(Member("ana@canonical.com")) == "member"
    assert [caller_label(c) for c in ("admin", "grants", None)] == [
        "admin",
        "grants",
        "unauthenticated",
    ]


# ---- The metrics port (FR-001, FR-010) ----------------------------------------


def test_hindsights_own_metrics_follow_the_gateways(gateway):
    """FR-010."""
    text = _scrape(gateway).text
    assert text.index("hindsight_gateway_requests_total") < text.index("hindsight_operation_total")
    assert "hindsight_gateway_llm_usage_up" not in text  # only on the leader


def test_the_metrics_port_serves_only_metrics(gateway):
    assert _scrape(gateway, "/v1/default/banks").status_code == 404
    response = _scrape(gateway)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")


# ---- LLM usage against PostgreSQL (FR-005 to FR-009) --------------------------


async def _record(url, *rows):
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        await connection.executemany(
            "INSERT INTO llm_requests (bank_id, provider, model, status, input_tokens,"
            " output_tokens, cached_tokens) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            rows,
        )
    finally:
        await connection.close()


async def _execute(url, sql):
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _call(bank, inp, out, cached=0, model="gpt-4o", status="success"):
    return (bank, "openai", model, status, inp, out, cached)


def test_totals_outlive_the_records_and_count_each_once(database):
    """FR-005, FR-006, SC-002."""

    async def scenario():
        usage = LlmUsage(database)
        try:
            await _record(database, _call("res-a", 100, 10, 40), _call("res-a", 50, 5))
            await usage.sweep()
            await usage.sweep()
            # Hindsight's retention deletes the records, then more calls come.
            await _execute(database, "DELETE FROM llm_requests")
            await _record(
                database,
                _call("res-a", 1, 1),
                _call(None, 7, 0),
                _call("res-b", None, None, status="error"),
            )
            await usage.sweep()
            return await usage.totals()
        finally:
            await usage.close()

    assert asyncio.run(scenario()) == [
        Usage("", "unattributed", "", "openai", "gpt-4o", 1, 7, 0, 0),
        Usage("res-a", "unattributed", "", "openai", "gpt-4o", 3, 151, 16, 40),
        Usage("res-b", "unattributed", "", "openai", "gpt-4o", 1, 0, 0, 0),
    ]


def test_counted_ids_are_forgotten_with_their_records(database):
    """FR-005: the bookkeeping does not grow past Hindsight's retention."""
    import asyncpg

    async def scenario():
        usage = LlmUsage(database)
        try:
            await _record(database, _call("res-a", 1, 1), _call("res-a", 1, 1))
            await usage.sweep()
            await _execute(database, "DELETE FROM llm_requests")
            await usage.sweep()
        finally:
            await usage.close()
        connection = await asyncpg.connect(database)
        try:
            return await connection.fetchval(
                "SELECT count(*) FROM hindsight_gateway.llm_usage_counted"
            )
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == 0


def test_two_gateways_sweeping_at_once_count_each_call_once(database):
    """FR-005, SC-002."""

    async def scenario():
        one, two = LlmUsage(database), LlmUsage(database)
        try:
            await _record(database, *[_call("res-a", 10, 1) for _ in range(50)])
            await asyncio.gather(*[u.sweep() for u in (one, two, one, two)])
            return await two.totals()
        finally:
            await one.close()
            await two.close()

    assert asyncio.run(scenario()) == [
        Usage("res-a", "unattributed", "", "openai", "gpt-4o", 50, 500, 50, 0)
    ]


def test_records_made_while_the_sweep_failed_are_counted_later(database):
    """FR-009, SC-002."""

    async def scenario():
        usage = LlmUsage(database)
        try:
            await _record(database, _call("res-a", 5, 5))
            await _execute(database, "ALTER TABLE llm_requests RENAME TO llm_requests_away")
            with pytest.raises(Exception, match="llm_requests"):
                await usage.sweep()
            await _execute(database, "ALTER TABLE llm_requests_away RENAME TO llm_requests")
            await usage.sweep()
            return await usage.totals()
        finally:
            await usage.close()

    assert asyncio.run(scenario()) == [
        Usage("res-a", "unattributed", "", "openai", "gpt-4o", 1, 5, 5, 0)
    ]


def test_hindsights_schema_is_read_when_it_is_not_public(database):
    """FR-005: HINDSIGHT_API_DATABASE_SCHEMA."""

    async def scenario():
        await _execute(
            database,
            'CREATE SCHEMA "Team A"; ALTER TABLE llm_requests SET SCHEMA "Team A"',
        )
        usage = LlmUsage(database, "Team A")
        try:
            await _execute(
                database,
                'INSERT INTO "Team A".llm_requests (bank_id, provider, model, input_tokens,'
                " output_tokens, cached_tokens) VALUES ('res-a', 'openai', 'gpt-4o', 2, 3, 0)",
            )
            await usage.sweep()
            return await usage.totals()
        finally:
            await usage.close()
            await _execute(database, 'DROP SCHEMA "Team A" CASCADE')

    assert asyncio.run(scenario()) == [
        Usage("res-a", "unattributed", "", "openai", "gpt-4o", 1, 2, 3, 0)
    ]


def test_the_leader_serves_the_totals_and_their_cost(database):
    """FR-007, FR-009."""

    async def seed():
        await _record(database, _call("res-a", 1000, 100, 400))

    asyncio.run(seed())
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS))
    app = create_app(
        policy,
        "http://api.test",
        transport=httpx.MockTransport(FakeHindsight()),
        metrics_port=0,
        llm_usage=LlmUsage(database),
        prices=PRICES,
        sweep_seconds=0.05,
    )
    with TestClient(app) as client:
        text = ""
        for _ in range(100):
            text = _scrape(client).text
            if "hindsight_gateway_llm_calls_total" in text:
                break
            time.sleep(0.05)
    base = 'bank="res-a",client="unattributed",operation="",provider="openai",model="gpt-4o"'
    assert "hindsight_gateway_llm_usage_up 1" in text
    assert f"hindsight_gateway_llm_calls_total{{{base}}} 1" in text
    assert f'hindsight_gateway_llm_tokens_total{{{base},type="input"}} 1000' in text
    assert f'hindsight_gateway_llm_tokens_total{{{base},type="cached"}} 400' in text
    assert f"hindsight_gateway_llm_cost_usd_total{{{base}}} 0.003" in text


def test_an_unreadable_database_reports_usage_down_and_keeps_counting():
    """FR-009."""
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS))
    app = create_app(
        policy,
        "http://api.test",
        transport=httpx.MockTransport(FakeHindsight()),
        metrics_port=0,
        llm_usage=LlmUsage("postgresql://nobody:x@127.0.0.1:1/none"),
        prices=PRICES,
        sweep_seconds=0.05,
    )
    with TestClient(app) as client:
        client.get("/v1/default/banks/alice-1/profile", headers=_as("k-alice"))
        text = _scrape(client).text
    assert "hindsight_gateway_llm_usage_up 0" in text
    assert _counts(text) == {_series("alice", "alice-1", "read", "2xx"): 1}


# ---- Prices (FR-007) ----------------------------------------------------------


def test_cached_input_is_priced_at_the_cache_price():
    usage = Usage("b", "unattributed", "", "openai", "gpt-4o", 1, 1000, 100, 400)
    assert cost(PRICES, usage) == pytest.approx(600 * 2.5e-06 + 400 * 1.25e-06 + 100 * 1e-05)


def test_a_model_is_looked_up_with_its_provider_first():
    usage = Usage("b", "unattributed", "", "gemini", "gemini-2.5-flash", 1, 1000, 0, 200)
    # No cache price in the list: cached tokens cost the input price.
    assert cost(PRICES, usage) == pytest.approx(1000 * 3e-07)


def test_an_unknown_model_has_tokens_but_no_cost():
    text = "\n".join(
        lines([Usage("b", "unattributed", "", "openai", "made-up", 1, 5, 5, 0)], PRICES)
    )
    series = (
        'bank="b",client="unattributed",operation="",provider="openai",model="made-up"'
        ',type="input"'
    )
    assert f"hindsight_gateway_llm_tokens_total{{{series}}} 5" in text
    assert "hindsight_gateway_llm_cost_usd_total" not in text


def test_unreadable_totals_are_reported_as_down():
    assert lines(None, PRICES) == [
        "# HELP hindsight_gateway_llm_usage_up Whether the LLM usage totals could be read.",
        "# TYPE hindsight_gateway_llm_usage_up gauge",
        "hindsight_gateway_llm_usage_up 0",
    ]


def test_the_bundled_price_list_is_found_without_importing_litellm(tmp_path, monkeypatch):
    from llm_usage import PRICE_FILE, bundled_price_file, load_prices

    package = tmp_path / "litellm"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('imported')\n")
    (package / PRICE_FILE).write_text(json.dumps(PRICES))
    monkeypatch.syspath_prepend(str(tmp_path))
    found = bundled_price_file()
    assert found == package / PRICE_FILE
    assert load_prices(found) == PRICES
    assert "litellm" not in sys.modules
