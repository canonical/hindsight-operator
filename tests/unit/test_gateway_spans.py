# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Trace context and spans (003 FR-013 to FR-016): the trace headers Hindsight
receives, and the spans exported in memory and to a stand-in OTLP endpoint."""

import http.server
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spans  # noqa: E402
from app import create_app  # noqa: E402
from grants import MemoryGrants  # noqa: E402
from policy import load_policy  # noqa: E402
from test_gateway_members import ADMIN, AUDIENCE, GRANTS_KEY, ISSUER, FakeIssuer  # noqa: E402

KEYS = {"alice": "k-alice"}
ACCESS = {"alice": {"banks": ["alice-*"]}}
BANKS = "/v1/default/banks"
CLIENT_TRACE = "0af7651916cd43dd8448eb211c80319c"
CLIENT_SPAN = "b7ad6b7169203331"
EMAIL = "ana@canonical.com"


class RecordingHindsight:
    """A stand-in API that keeps the headers of every request it receives."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        status = 500 if request.url.path.endswith("/boom") else 200
        return httpx.Response(
            status,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(b'{"ok": true}'),
        )


@pytest.fixture
def upstream():
    return RecordingHindsight()


@pytest.fixture
def issuer():
    return FakeIssuer()


@pytest.fixture
def exported():
    return InMemorySpanExporter()


def _app(upstream, issuer, tracer_provider):
    grants = MemoryGrants()
    grants.rows = {"res-a": {EMAIL}}
    policy = load_policy(ADMIN, KEYS, json.dumps(ACCESS), grants_key=GRANTS_KEY)
    return create_app(
        policy,
        "http://api.test",
        transport=httpx.MockTransport(upstream),
        member_issuer=ISSUER,
        member_audience=AUDIENCE,
        grants=grants,
        issuer_transport=httpx.MockTransport(issuer),
        tracer_provider=tracer_provider,
    )


@pytest.fixture
def gateway(upstream, issuer, exported):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exported))
    with TestClient(_app(upstream, issuer, tracer_provider)) as client:
        yield client


def _as(key, traceparent=None):
    headers = {"Authorization": f"Bearer {key}"}
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


def _parent(request: httpx.Request) -> tuple[str, str, str]:
    """The trace ID, parent span ID and flags of the traceparent Hindsight received."""
    version, trace_id, span_id, flags = request.headers["traceparent"].split("-")
    assert version == "00"
    return trace_id, span_id, flags


def test_a_client_trace_is_continued_through_the_gateway(gateway, upstream, exported):
    """FR-013, FR-015, SC-004: Hindsight's parent is the gateway's span, in the client's trace."""
    traceparent = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-01"
    response = gateway.post(
        f"{BANKS}/alice-1/memories",
        headers={**_as("k-alice", traceparent), "tracestate": "vendor=1", "baggage": "team=x"},
        json={},
    )
    assert response.status_code == 200
    (span,) = exported.get_finished_spans()
    assert format(span.context.trace_id, "032x") == CLIENT_TRACE
    assert format(span.parent.span_id, "016x") == CLIENT_SPAN
    assert span.kind is SpanKind.SERVER
    (forwarded,) = upstream.seen
    trace_id, parent_id, flags = _parent(forwarded)
    assert (trace_id, flags) == (CLIENT_TRACE, "01")
    assert parent_id == format(span.context.span_id, "016x") != CLIENT_SPAN
    # The client's tracestate goes on in the gateway's span context, and baggage unchanged.
    assert forwarded.headers["tracestate"] == "vendor=1"
    assert forwarded.headers["baggage"] == "team=x"
    assert forwarded.headers.get_list("traceparent") == [forwarded.headers["traceparent"]]


@pytest.mark.parametrize(
    "traceparent", [None, "00-not-a-trace-01", f"00-{'0' * 32}-{CLIENT_SPAN}-01"]
)
def test_a_request_without_a_valid_trace_starts_one(gateway, upstream, exported, traceparent):
    """FR-013, SC-004."""
    gateway.post(f"{BANKS}/alice-1/memories", headers=_as("k-alice", traceparent), json={})
    (span,) = exported.get_finished_spans()
    assert span.parent is None
    trace_id, parent_id, flags = _parent(upstream.seen[0])
    assert trace_id == format(span.context.trace_id, "032x") != "0" * 32
    assert parent_id == format(span.context.span_id, "016x")
    # Sampled; newer SDKs also set the W3C level 2 random trace ID flag.
    assert int(flags, 16) & 1


def test_an_unsampled_trace_is_passed_on_and_not_exported(gateway, upstream, exported):
    """Edge case: the client's sampled flag decides."""
    traceparent = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-00"
    gateway.post(f"{BANKS}/alice-1/memories", headers=_as("k-alice", traceparent), json={})
    assert exported.get_finished_spans() == ()
    trace_id, parent_id, flags = _parent(upstream.seen[0])
    assert (trace_id, flags) == (CLIENT_TRACE, "00")
    assert parent_id != CLIENT_SPAN


def test_a_public_route_has_no_span_and_keeps_the_client_headers(gateway, upstream, exported):
    """Edge case: public routes are not counted, so they get no span."""
    traceparent = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-01"
    gateway.get("/health", headers={"traceparent": traceparent})
    assert exported.get_finished_spans() == ()
    assert upstream.seen[0].headers["traceparent"] == traceparent


def test_the_span_names_the_caller_and_never_a_key_token_or_email(
    gateway, upstream, issuer, exported
):
    """FR-014, SC-004: the attributes of a client's, a member's and a refused request."""
    token = issuer.token()
    gateway.post(f"{BANKS}/alice-1/memories/recall", headers=_as("k-alice"), json={})
    gateway.post(f"{BANKS}/res-a/reflect", headers=_as(token), json={})
    gateway.get(f"{BANKS}/secret-bank/profile", headers=_as("k-alice"))
    gateway.get(f"{BANKS}/alice-1/profile", headers=_as("wrong-key"))
    recall, reflect, refused, unknown = exported.get_finished_spans()
    assert recall.name == "gateway recall"
    assert dict(recall.attributes) == {
        "hindsight.client": "alice",
        "hindsight.bank": "alice-1",
        "hindsight.operation": "recall",
        "http.request.method": "POST",
        "http.response.status_code": 200,
    }
    assert reflect.attributes["hindsight.client"] == "member"
    assert reflect.attributes["hindsight.bank"] == "res-a"
    # FR-003 applies to spans as to metrics: a refused request names no bank.
    assert refused.attributes["http.response.status_code"] == 403
    assert "hindsight.bank" not in refused.attributes
    assert unknown.attributes["hindsight.client"] == "unauthenticated"
    everything = repr([(s.name, dict(s.attributes)) for s in exported.get_finished_spans()])
    for secret in ("k-alice", "wrong-key", token, EMAIL, "/v1/default", "secret-bank"):
        assert secret.lower() not in everything.lower()


def test_a_server_error_marks_the_span(gateway, exported):
    gateway.get(f"{BANKS}/alice-1/boom", headers=_as("k-alice"))
    (span,) = exported.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 500


def test_the_gateway_s_own_calls_for_a_request_carry_its_span(gateway, upstream, exported):
    """FR-015: the grants API's bank check is part of the request's trace."""
    traceparent = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-01"
    response = gateway.put("/admin/banks/res-z", headers=_as(GRANTS_KEY, traceparent))
    assert response.status_code == 200
    (span,) = exported.get_finished_spans()
    assert span.attributes["hindsight.client"] == "grants"
    (check,) = upstream.seen
    assert check.url.path == f"{BANKS}/res-z/profile"
    assert _parent(check) == (CLIENT_TRACE, format(span.context.span_id, "016x"), "01")


# ---- Export over OTLP HTTP (FR-016) ------------------------------------------


class _Collector(http.server.BaseHTTPRequestHandler):
    received: list[tuple[str, str, bytes]] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["content-length"]))
        self.received.append((self.path, self.headers["content-type"], body))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def collector(monkeypatch):
    """A stand-in for Tempo's OTLP HTTP receiver."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    _Collector.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", _Collector.received
    server.shutdown()


def _exported_spans(received):
    out = []
    for path, content_type, body in received:
        assert path == "/v1/traces"
        assert content_type == "application/x-protobuf"
        message = ExportTraceServiceRequest()
        message.ParseFromString(body)
        for resource_spans in message.resource_spans:
            resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    attributes = {a.key: a.value for a in span.attributes}
                    out.append((resource, span, attributes))
    return out


def test_spans_are_exported_to_the_otlp_endpoint(upstream, issuer, collector):
    """FR-016, SC-004: the spans reach the endpoint when the gateway stops, at the latest."""
    endpoint, received = collector
    tracer_provider = spans.provider(spans.otlp_exporter(endpoint))
    traceparent = f"00-{CLIENT_TRACE}-{CLIENT_SPAN}-01"
    with TestClient(_app(upstream, issuer, tracer_provider)) as client:
        client.post(f"{BANKS}/alice-1/memories", headers=_as("k-alice", traceparent), json={})
    ((resource, span, attributes),) = _exported_spans(received)
    assert resource["service.name"] == "hindsight-gateway"
    assert span.trace_id.hex() == CLIENT_TRACE
    assert span.parent_span_id.hex() == CLIENT_SPAN
    assert attributes["hindsight.client"].string_value == "alice"
    assert attributes["hindsight.operation"].string_value == "retain"
    assert b"k-alice" not in received[0][2]
    assert ADMIN.encode() not in received[0][2]


def test_the_environment_turns_export_on(collector, monkeypatch):
    """FR-016: the charm's HINDSIGHT_GATEWAY_OTLP_ENDPOINT, and no export without it."""
    import app as gateway_app

    endpoint, received = collector
    monkeypatch.setenv("HINDSIGHT_GATEWAY_ADMIN_KEY", ADMIN)
    monkeypatch.setenv("HINDSIGHT_GATEWAY_KEYS", json.dumps(KEYS))
    monkeypatch.setenv("HINDSIGHT_GATEWAY_ACCESS", json.dumps(ACCESS))
    # Never reached: an unknown key is refused by the gateway itself.
    monkeypatch.setenv("HINDSIGHT_GATEWAY_UPSTREAM", "http://127.0.0.1:9")
    monkeypatch.setattr(gateway_app, "METRICS_PORT", 0)
    for endpoint_value, expected in (("", 0), (endpoint, 1)):
        monkeypatch.setenv("HINDSIGHT_GATEWAY_OTLP_ENDPOINT", endpoint_value)
        received.clear()
        with TestClient(gateway_app.app_factory()) as client:
            assert client.get(f"{BANKS}/alice-1/profile", headers=_as("wrong")).status_code == 401
        assert len(_exported_spans(received)) == expected
