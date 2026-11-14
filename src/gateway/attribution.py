# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Which caller retained each document (003 FR-018 to FR-021).

Hindsight 0.9.2 starts a trace of its own for each operation, so its LLM call
records cannot be joined to the gateway's spans by trace. A retain's extraction
calls do carry the document they extract from, in ``metadata.document_id``.
Before forwarding a retain, every unit's gateway records each document the
request names with the caller and the request's span, in the
``hindsight_gateway`` schema. The leader's sweep (``llm_usage.py``) joins the
records to Hindsight's LLM calls by bank and document.

The gateway reads the retain body and never changes it. A retain item without
a document ID gets one from Hindsight that the gateway never sees, so its calls
stay unattributed.

This module runs inside the Hindsight image (Python 3.11), on its asyncpg.
"""

from __future__ import annotations

import asyncio
import json
import logging

from grants import SCHEMA, GrantsUnavailableError, resolve_database_url

logger = logging.getLogger("hindsight.gateway")

# Longer than any retain should queue for before Hindsight extracts it.
KEEP = "7 days"
# A retain is forwarded after this long even when its record cannot be written.
WRITE_SECONDS = 2.0
_MCP_RETAIN_TOOLS = frozenset({"retain", "sync_retain"})

CREATE = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.retained_documents (
    bank_id text NOT NULL,
    document_id text NOT NULL,
    client text NOT NULL,
    trace_id text NOT NULL,
    span_id text NOT NULL,
    sampled boolean NOT NULL,
    retained_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bank_id, document_id)
);
"""

_RECORD = f"""
INSERT INTO {SCHEMA}.retained_documents AS d
    (bank_id, document_id, client, trace_id, span_id, sampled)
SELECT $1, unnest($2::text[]), $3, $4, $5, $6
ON CONFLICT (bank_id, document_id) DO UPDATE SET
    client = EXCLUDED.client,
    trace_id = EXCLUDED.trace_id,
    span_id = EXCLUDED.span_id,
    sampled = EXCLUDED.sampled,
    retained_at = now()
"""


def _document_id(value) -> str | None:
    return value if isinstance(value, str) and value else None


def document_ids(op: str, mcp: bool, body: bytes) -> list[str]:
    """The document IDs a retain names, in the order given; none for anything else."""
    if op not in ("retain", "mcp"):
        return []
    try:
        message = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return []
    found: list[str | None] = []
    if mcp:
        for item in message if isinstance(message, list) else [message]:
            if not isinstance(item, dict) or item.get("method") != "tools/call":
                continue
            params = item.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                continue
            if params["name"] in _MCP_RETAIN_TOOLS:
                arguments = params.get("arguments")
                if isinstance(arguments, dict):
                    found.append(_document_id(arguments.get("document_id")))
    elif isinstance(message, dict) and isinstance(message.get("items"), list):
        found = [
            _document_id(item.get("document_id"))
            for item in message["items"]
            if isinstance(item, dict)
        ]
    return list(dict.fromkeys(d for d in found if d is not None))


class RetainedDocuments:
    """The retained documents in Hindsight's PostgreSQL. The pool opens on first use."""

    def __init__(self, database_url: str, write_seconds: float = WRITE_SECONDS):
        self._database_url = database_url
        self._write_seconds = write_seconds
        self._pool = None
        self._lock = asyncio.Lock()

    async def _connected(self):
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                import asyncpg

                try:
                    dsn = await asyncio.get_running_loop().run_in_executor(
                        None, resolve_database_url, self._database_url
                    )
                    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
                    async with pool.acquire() as connection:
                        try:
                            await connection.execute(CREATE)
                        except asyncpg.UniqueViolationError:
                            # Another unit created the table at the same moment.
                            await connection.execute(CREATE)
                except (OSError, asyncpg.PostgresError, GrantsUnavailableError) as exc:
                    raise GrantsUnavailableError(str(exc)) from exc
                self._pool = pool
        return self._pool

    async def _write(self, bank, documents, client, trace_id, span_id, sampled) -> None:
        import asyncpg

        pool = await self._connected()
        try:
            await pool.execute(_RECORD, bank, documents, client, trace_id, span_id, sampled)
        except (OSError, asyncpg.PostgresError) as exc:
            raise GrantsUnavailableError(str(exc)) from exc

    async def record(
        self,
        bank: str,
        documents: list[str],
        client: str,
        trace_id: str,
        span_id: str,
        sampled: bool,
    ) -> None:
        """Record who retained ``documents``. A failure is logged, never raised (FR-021)."""
        if not documents:
            return
        try:
            await asyncio.wait_for(
                self._write(bank, documents, client, trace_id, span_id, sampled),
                self._write_seconds,
            )
        except (GrantsUnavailableError, TimeoutError) as exc:
            logger.warning("retained documents not recorded: %s", exc or "timed out")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
