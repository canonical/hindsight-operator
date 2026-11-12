# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""LLM calls attributed to the caller that retained their document (003 FR-018 to
FR-021): the documents recorded per retain, the sweep's join against a real
``llm_requests`` table in PostgreSQL, and the spans of the attributed calls."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spans  # noqa: E402
from app import create_app  # noqa: E402
from attribution import RetainedDocuments, document_ids  # noqa: E402
from grants import MemoryGrants  # noqa: E402
from llm_usage import LlmCall, LlmUsage, Usage  # noqa: E402
from policy import load_policy  # noqa: E402
from test_gateway_members import ADMIN, AUDIENCE, ISSUER, FakeIssuer  # noqa: E402
from test_gateway_spans import CLIENT_SPAN, CLIENT_TRACE, RecordingHindsight  # noqa: E402

KEYS = {"alice": "k-alice"}
ACCESS = {"alice": {"banks": ["alice-*"]}}
MEMORIES = "/v1/default/banks/alice-1/memories"
MCP = "/mcp/alice-1/"
SAMPLED = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-01"
UNSAMPLED = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-00"


def _retain(*document_ids, async_=False) -> dict:
    items = [{"content": f"fact {i}"} for i in range(len(document_ids))]
    for item, document in zip(items, document_ids, strict=True):
        if document is not None:
            item["document_id"] = document
    return {"items": items, "async": async_}


def _mcp(*calls) -> list[dict]:
    return [
        {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}}
        for i, (n, a) in enumerate(calls)
    ]


# ---- The documents a retain names (FR-018, FR-021) ---------------------------


def test_a_retain_names_the_document_ids_of_its_items():
    body = json.dumps(_retain("doc-a", None, "doc-b", "doc-a")).encode()
    assert document_ids("retain", False, body) == ["doc-a", "doc-b"]


def test_mcp_retain_tools_name_their_document_ids():
    body = json.dumps(
        _mcp(
            ("retain", {"content": "x", "document_id": "doc-a"}),
            ("sync_retain", {"content": "y", "document_id": "doc-b"}),
            ("recall", {"query": "q", "document_id": "not-a-retain"}),
            ("retain", {"content": "z"}),
            (["retain"], {"content": "w", "document_id": "unhashable-name"}),
        )
    ).encode()
    assert document_ids("mcp", True, body) == ["doc-a", "doc-b"]
    single = json.dumps(_mcp(("retain", {"content": "x", "document_id": "doc-c"}))[0]).encode()
    assert document_ids("retain", True, single) == ["doc-c"]


@pytest.mark.parametrize(
    ("op", "body"),
    [
        ("recall", json.dumps(_retain("doc-a")).encode()),
        ("retain", b"not json"),
        ("retain", b'{"items": "nope"}'),
        ("retain", json.dumps({"items": [{"content": "x", "document_id": 7}]}).encode()),
        ("retain", json.dumps({"items": [{"content": "x", "document_id": ""}]}).encode()),
    ],
)
def test_anything_else_names_no_document(op, body):
    assert document_ids(op, False, body) == []


# ---- What the gateway records before forwarding a retain (FR-018, FR-021) -----


class RecordingDocuments:
    """Stands in for :class:`RetainedDocuments`, noting what Hindsight had seen at each record."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.records: list[tuple] = []

    async def record(self, bank, documents, client, trace_id, span_id, sampled):
        self.records.append(
            (bank, documents, client, trace_id, span_id, sampled, len(self.upstream.seen))
        )

    async def close(self):
        pass


@pytest.fixture
def upstream():
    return RecordingHindsight()


@pytest.fixture
def issuer():
    return FakeIssuer()


@pytest.fixture
def exported():
    return InMemorySpanExporter()


def _app(upstream, issuer, exported, retained, **extra):
    grants = MemoryGrants()
    grants.rows = {"alice-1": {"ana@canonical.com"}}
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exported))
    return create_app(
        load_policy(ADMIN, KEYS, json.dumps(ACCESS)),
        "http://api.test",
        transport=httpx.MockTransport(upstream),
        member_issuer=ISSUER,
        member_audience=AUDIENCE,
        grants=grants,
        issuer_transport=httpx.MockTransport(issuer),
        tracer_provider=tracer_provider,
        retained=retained,
        **extra,
    )


def _headers(key, traceparent=SAMPLED):
    return {"Authorization": f"Bearer {key}", "traceparent": traceparent}


def test_a_retain_is_recorded_with_its_caller_and_span_before_it_is_forwarded(
    upstream, issuer, exported
):
    """FR-018: the record exists before Hindsight can call an LLM for the retain."""
    retained = RecordingDocuments(upstream)
    body = json.dumps(_retain("doc-a", "doc-b", None)).encode()
    with TestClient(_app(upstream, issuer, exported, retained)) as client:
        response = client.post(MEMORIES, content=body, headers=_headers("k-alice"))
    assert response.status_code == 200
    (span,) = exported.get_finished_spans()
    assert retained.records == [
        (
            "alice-1",
            ["doc-a", "doc-b"],
            "alice",
            CLIENT_TRACE,
            format(span.context.span_id, "016x"),
            True,
            0,
        )
    ]
    # FR-021: the body reaches Hindsight as the client sent it.
    assert upstream.seen[0].content == body


def test_admins_members_and_mcp_retains_are_recorded_by_caller_label(upstream, issuer, exported):
    """FR-018, with the caller labels of FR-002: never an email."""
    retained = RecordingDocuments(upstream)
    body = json.dumps(_retain("doc-a")).encode()
    mcp = json.dumps(_mcp(("retain", {"content": "x", "document_id": "doc-m"}))).encode()
    with TestClient(_app(upstream, issuer, exported, retained)) as client:
        client.post(MEMORIES, content=body, headers=_headers(ADMIN, UNSAMPLED))
        client.post(MEMORIES, content=body, headers=_headers(issuer.token()))
        client.post(MCP, content=mcp, headers=_headers("k-alice"))
        client.post(MCP, content=mcp, headers=_headers(ADMIN))
    assert [(r[1], r[2], r[5]) for r in retained.records] == [
        (["doc-a"], "admin", False),
        (["doc-a"], "member", True),
        (["doc-m"], "alice", True),
        (["doc-m"], "admin", True),
    ]


def test_nothing_is_recorded_for_refused_requests_or_other_operations(upstream, issuer, exported):
    """FR-018: only a retain the caller may make."""
    retained = RecordingDocuments(upstream)
    body = json.dumps(_retain("doc-a")).encode()
    with TestClient(_app(upstream, issuer, exported, retained)) as client:
        assert (
            client.post(
                "/v1/default/banks/bob-1/memories", content=body, headers=_headers("k-alice")
            ).status_code
            == 403
        )
        assert client.post(MEMORIES, content=body, headers=_headers("wrong")).status_code == 401
        client.post(MEMORIES + "/recall", json={"query": "q"}, headers=_headers("k-alice"))
        client.post(MEMORIES, json=_retain(None), headers=_headers("k-alice"))
    assert retained.records == []


def test_a_retain_is_forwarded_when_its_record_cannot_be_written(upstream, issuer, exported):
    """FR-021."""
    retained = RetainedDocuments("postgresql://nobody:x@127.0.0.1:1/none", write_seconds=1.0)
    with TestClient(_app(upstream, issuer, exported, retained)) as client:
        response = client.post(MEMORIES, json=_retain("doc-a"), headers=_headers("k-alice"))
    assert response.status_code == 200
    assert len(upstream.seen) == 1


# ---- The sweep's join (FR-019) -------------------------------------------------

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


async def _llm_call(url, bank, metadata, operation="retain", inp=100, out=10, status="success"):
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        await connection.execute(
            "INSERT INTO llm_requests (bank_id, operation, provider, model, status, started_at,"
            " ended_at, input_tokens, output_tokens, cached_tokens, metadata)"
            " VALUES ($1, $2, 'openai', 'gpt-4o', $3, $4, $5, $6, $7, 0, $8::jsonb)",
            bank,
            operation,
            status,
            T0,
            T0 + timedelta(seconds=2),
            inp,
            out,
            json.dumps(metadata),
        )
    finally:
        await connection.close()


async def _fetchval(url, sql):
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        return await connection.fetchval(sql)
    finally:
        await connection.close()


TRACE_A, SPAN_A = "a" * 32, "1" * 16
TRACE_B, SPAN_B = "b" * 32, "2" * 16


def test_calls_are_attributed_to_whoever_retained_their_document(database):
    """FR-019, SC-006."""

    async def scenario():
        retained, usage = RetainedDocuments(database), LlmUsage(database)
        try:
            await retained.record("res-a", ["doc-1"], "alice", TRACE_A, SPAN_A, True)
            await retained.record("res-a", ["doc-2"], "member", TRACE_B, SPAN_B, False)
            await _llm_call(database, "res-a", {"document_id": "doc-1"})
            await _llm_call(database, "res-a", {"document_id": "doc-1"}, inp=50, out=5)
            await _llm_call(database, "res-a", {"document_id": "doc-2"})
            # The same document ID in another bank is not the same document.
            await _llm_call(database, "res-b", {"document_id": "doc-1"})
            await _llm_call(database, "res-a", {}, operation="reflect", inp=7, out=7)
            # What Hindsight keeps of a document ID too long for the trace limit.
            await _llm_call(
                database,
                "res-a",
                {"_truncated": True, "_original_chars": 300, "preview": '{"document_id": "doc-1'},
            )
            calls = await usage.sweep()
            return calls, await usage.totals()
        finally:
            await retained.close()
            await usage.close()

    calls, totals = asyncio.run(scenario())
    assert totals == [
        Usage("res-a", "alice", "retain", "openai", "gpt-4o", 2, 150, 15, 0),
        Usage("res-a", "member", "retain", "openai", "gpt-4o", 1, 100, 10, 0),
        Usage("res-a", "unattributed", "reflect", "openai", "gpt-4o", 1, 7, 7, 0),
        Usage("res-a", "unattributed", "retain", "openai", "gpt-4o", 1, 100, 10, 0),
        Usage("res-b", "unattributed", "retain", "openai", "gpt-4o", 1, 100, 10, 0),
    ]
    # FR-020: only the calls of a sampled trace come back for spans.
    assert [(c.client, c.trace_id, c.span_id, c.input_tokens) for c in calls] == [
        ("alice", TRACE_A, SPAN_A, 100),
        ("alice", TRACE_A, SPAN_A, 50),
    ]


def test_a_newer_retain_of_a_document_takes_its_later_calls(database):
    """FR-018: the latest retain of a bank's document wins."""

    async def scenario():
        retained, usage = RetainedDocuments(database), LlmUsage(database)
        try:
            await retained.record("res-a", ["doc-1"], "alice", TRACE_A, SPAN_A, True)
            await _llm_call(database, "res-a", {"document_id": "doc-1"})
            await usage.sweep()
            await retained.record("res-a", ["doc-1"], "admin", TRACE_B, SPAN_B, True)
            await _llm_call(database, "res-a", {"document_id": "doc-1"})
            calls = await usage.sweep()
            return calls, await usage.totals()
        finally:
            await retained.close()
            await usage.close()

    calls, totals = asyncio.run(scenario())
    assert [(u.client, u.calls) for u in totals] == [("admin", 1), ("alice", 1)]
    assert [(c.client, c.trace_id) for c in calls] == [("admin", TRACE_B)]


def test_retained_documents_are_forgotten_after_a_week(database):
    """FR-018."""

    async def scenario():
        retained, usage = RetainedDocuments(database), LlmUsage(database)
        try:
            await retained.record("res-a", ["old", "new"], "alice", TRACE_A, SPAN_A, True)
            await _execute_sql(
                database,
                "UPDATE hindsight_gateway.retained_documents"
                " SET retained_at = now() - interval '8 days' WHERE document_id = 'old'",
            )
            await usage.sweep()
            return await _fetchval(
                database,
                "SELECT string_agg(document_id, ',') FROM hindsight_gateway.retained_documents",
            )
        finally:
            await retained.close()
            await usage.close()

    assert asyncio.run(scenario()) == "new"


async def _execute_sql(url, sql):
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


# ---- The spans of attributed calls (FR-020) ----------------------------------


def _call(**changes) -> LlmCall:
    values = dict(
        bank="res-a",
        client="alice",
        operation="retain",
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=10,
        cached_tokens=4,
        status="success",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=2),
        trace_id=CLIENT_TRACE,
        span_id=CLIENT_SPAN,
    )
    values.update(changes)
    return LlmCall(**values)


def test_an_attributed_call_is_a_span_under_its_retain(exported):
    """FR-020: the call's own times, model and tokens, and no document or prompt."""
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exported))
    tracer = tracer_provider.get_tracer("test")
    spans.llm_calls(tracer, [_call(), _call(status="error", ended_at=None)])
    ok, failed = exported.get_finished_spans()
    assert ok.name == "hindsight llm retain"
    assert ok.kind is SpanKind.CLIENT
    assert format(ok.context.trace_id, "032x") == CLIENT_TRACE
    assert format(ok.parent.span_id, "016x") == CLIENT_SPAN
    assert ok.start_time == int(T0.timestamp() * 1e9)
    assert ok.end_time - ok.start_time == 2_000_000_000
    assert dict(ok.attributes) == {
        "hindsight.client": "alice",
        "hindsight.bank": "res-a",
        "hindsight.operation": "retain",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 10,
        "gen_ai.usage.cached_tokens": 4,
    }
    assert ok.status.status_code is StatusCode.UNSET
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.end_time == failed.start_time


def test_a_retain_traced_through_the_gateway_reaches_its_llm_calls(
    database, upstream, issuer, exported
):
    """FR-018 to FR-020, SC-006: the client's trace holds the gateway span and the LLM call."""
    usage = LlmUsage(database)
    retained = RetainedDocuments(database)
    app = _app(
        upstream, issuer, exported, retained, llm_usage=usage, sweep_seconds=0.05, prices={}
    )
    with TestClient(app) as client:
        response = client.post(MEMORIES, json=_retain("doc-t"), headers=_headers("k-alice"))
        assert response.status_code == 200
        asyncio.run(_llm_call(database, "alice-1", {"document_id": "doc-t"}))
        for _ in range(100):
            if len(exported.get_finished_spans()) == 2:
                break
            time.sleep(0.05)
    gateway, llm = exported.get_finished_spans()
    assert gateway.name == "gateway retain"
    assert llm.name == "hindsight llm retain"
    assert llm.context.trace_id == gateway.context.trace_id
    assert format(llm.context.trace_id, "032x") == CLIENT_TRACE
    assert llm.parent.span_id == gateway.context.span_id
    assert llm.attributes["hindsight.client"] == "alice"
    assert "doc-t" not in json.dumps(dict(llm.attributes))
