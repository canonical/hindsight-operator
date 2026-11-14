# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Request counts and the metrics port (003 FR-001 to FR-004, FR-010).

The gateway counts each request it answers in memory, and serves the counts,
the LLM usage totals (``llm_usage.py``) and Hindsight's own metrics in the
Prometheus text format on a port the ingress does not publish.

This module runs inside the Hindsight image (Python 3.11).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable

from policy import Client, Kind, Member, Request, mcp_tools_called

logger = logging.getLogger("hindsight.gateway")

METRICS_PORT = 9464
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Bank sub-paths whose POST is one of Hindsight's named operations.
_NAMED_POSTS = {"memories": "retain", "memories/recall": "recall", "reflect": "reflect"}
_MCP_TOOLS = {
    "retain": "retain",
    "sync_retain": "retain",
    "recall": "recall",
    "reflect": "reflect",
}


def caller_label(caller: Client | Member | str | None) -> str:
    """The caller as a metric label: never a key, a token or an email (FR-002)."""
    if isinstance(caller, Client):
        return caller.name
    if isinstance(caller, Member):
        return "member"
    if caller in ("admin", "grants"):
        return caller
    return "unauthenticated"


def operation(method: str, path: str, request: Request, mcp_body: bytes | None = None) -> str:
    """The operation of a request, from a fixed vocabulary (FR-004)."""
    kind = request.kind
    if kind is Kind.INVALID:
        return "invalid"
    if kind is Kind.BANK_LIST:
        return "list-banks"
    if kind is Kind.MCP_BANK:
        tools = mcp_tools_called(mcp_body) if mcp_body is not None else None
        named = {_MCP_TOOLS.get(tool) for tool in tools or ()}
        return named.pop() if len(named) == 1 and None not in named else "mcp"
    if kind in (Kind.BANK_READ, Kind.BANK_WRITE):
        if request.deletes_bank:
            return "delete-bank"
        rest = path.split("/", 5)[5] if path.count("/") >= 5 else ""
        if method.upper() == "POST" and rest.rstrip("/") in _NAMED_POSTS:
            return _NAMED_POSTS[rest.rstrip("/")]
        return "read" if kind is Kind.BANK_READ else "write"
    return "admin"


class RequestCounts:
    """Requests answered, by caller, bank, operation and status class."""

    def __init__(self):
        self._counts: Counter[tuple[str, str, str, str]] = Counter()

    def add(self, caller: str, bank: str, op: str, status: int) -> None:
        self._counts[(caller, bank, op, f"{status // 100}xx")] += 1

    def lines(self) -> list[str]:
        out = [
            "# HELP hindsight_gateway_requests_total Requests the gateway answered.",
            "# TYPE hindsight_gateway_requests_total counter",
        ]
        for (caller, bank, op, code), count in sorted(self._counts.items()):
            label_set = labels(client=caller, bank=bank, operation=op, code=code)
            out.append(f"hindsight_gateway_requests_total{label_set} {count}")
        return out


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels(**values: str) -> str:
    """A Prometheus label set, escaped."""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in values.items()) + "}"


async def serve_metrics(
    render: Callable[[], Awaitable[Iterable[str]]], host: str = "0.0.0.0", port: int = METRICS_PORT
) -> asyncio.base_events.Server:
    """Listen for ``GET /metrics`` and answer it with ``render()``.

    A plain asyncio listener in the gateway's own process, so it reads the
    same counters as the proxy. It answers one request per connection.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            method, target, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            if method not in ("GET", "HEAD") or target.split("?", 1)[0] != "/metrics":
                status, body = "404 Not Found", b"not found\n"
            else:
                status = "200 OK"
                body = ("\n".join(await render()) + "\n").encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {CONTENT_TYPE}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            )
            if method != "HEAD":
                writer.write(body)
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, ValueError, OSError):
            pass
        except Exception:  # a broken render must not stop the listener
            logger.exception("metrics request failed")
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
