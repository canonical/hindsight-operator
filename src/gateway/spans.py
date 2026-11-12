# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Trace context and the gateway's spans (003 FR-013 to FR-016, FR-020).

The gateway continues the W3C trace a client starts, records a span for each
request it counts, and names that span as the parent of its call to Hindsight.
The leader adds a span for each LLM call it attributes to a retain, under that
retain's span, once Hindsight has recorded the call. The spans are exported
over OTLP HTTP when the charm gives an endpoint.

This module runs inside the Hindsight image (Python 3.11), on its OpenTelemetry SDK.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Mapping
from datetime import datetime

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

SERVICE_NAME = "hindsight-gateway"
# The headers the gateway replaces on the way to Hindsight. `baggage` passes as it came.
TRACE_HEADERS = frozenset({"traceparent", "tracestate"})

_PROPAGATOR = TraceContextTextMapPropagator()


def provider(exporter: SpanExporter | None = None) -> TracerProvider:
    """A provider of the gateway's own, never the global one; exporting only with ``exporter``."""
    resource = Resource.create(
        {"service.name": SERVICE_NAME, "service.instance.id": socket.gethostname()}
    )
    tracer_provider = TracerProvider(resource=resource)
    if exporter is not None:
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    return tracer_provider


def otlp_exporter(endpoint: str) -> SpanExporter:
    """An OTLP HTTP exporter to the tracing provider's receiver."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")


def start(tracer: trace.Tracer, headers: Mapping[str, str]) -> Span:
    """A span for one request, in the client's trace if it sent a valid one (FR-013)."""
    parent = _PROPAGATOR.extract(headers)
    return tracer.start_span("gateway", context=parent, kind=SpanKind.SERVER)


def outgoing(span: Span) -> list[tuple[str, str]]:
    """The trace headers that make ``span`` the parent of the call to Hindsight (FR-015)."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier, context=trace.set_span_in_context(span))
    return sorted(carrier.items())


def finish(span: Span, method: str, caller: str, bank: str, op: str, status: int | None) -> None:
    """Name the span by its operation and end it, with no key, token, email or path (FR-014)."""
    span.update_name(f"gateway {op}")
    span.set_attribute("hindsight.client", caller)
    if bank:
        span.set_attribute("hindsight.bank", bank)
    span.set_attribute("hindsight.operation", op)
    span.set_attribute("http.request.method", method)
    if status is not None:
        span.set_attribute("http.response.status_code", status)
    if status is None or status >= 500:
        span.set_status(Status(StatusCode.ERROR))
    span.end()


def identity(span: Span) -> tuple[str, str, bool]:
    """The span's trace ID and span ID in hex, and whether its trace is sampled."""
    context = span.get_span_context()
    return (
        trace.format_trace_id(context.trace_id),
        trace.format_span_id(context.span_id),
        context.trace_flags.sampled,
    )


def _nanoseconds(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def llm_calls(tracer: trace.Tracer, calls: Iterable) -> None:
    """A span for each attributed LLM call, under the span of its retain (FR-020).

    ``calls`` are ``llm_usage.LlmCall``. A span carries the call's times, model
    and tokens, and no prompt, output or document ID.
    """
    for call in calls:
        parent = SpanContext(
            trace_id=int(call.trace_id, 16),
            span_id=int(call.span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        if not parent.is_valid:
            continue
        span = tracer.start_span(
            f"hindsight llm {call.operation}".rstrip(),
            context=trace.set_span_in_context(NonRecordingSpan(parent)),
            kind=SpanKind.CLIENT,
            start_time=_nanoseconds(call.started_at),
            attributes={
                "hindsight.client": call.client,
                "hindsight.bank": call.bank,
                "hindsight.operation": call.operation,
                "gen_ai.system": call.provider,
                "gen_ai.request.model": call.model,
                "gen_ai.usage.input_tokens": call.input_tokens,
                "gen_ai.usage.output_tokens": call.output_tokens,
                "gen_ai.usage.cached_tokens": call.cached_tokens,
            },
        )
        if call.status != "success":
            span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=_nanoseconds(call.ended_at or call.started_at))
