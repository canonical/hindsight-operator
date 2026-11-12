# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""LLM calls, tokens and cost per bank and client (003 FR-005 to FR-009, FR-019, FR-020).

Hindsight records each LLM call in its ``llm_requests`` table and deletes the
records after a day. The leader's gateway adds every record to running totals
in the ``hindsight_gateway`` schema, once, so the totals outlive the records
(architecture.md§10). A call extracting a document a caller retained through
the gateway (``attribution.py``) is added to that caller's total, and every
other call to ``unattributed``. The cost is priced when the metrics are read,
with the LiteLLM price list bundled in the Hindsight image.

This module runs inside the Hindsight image (Python 3.11), on its asyncpg.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from attribution import CREATE as CREATE_RETAINED
from attribution import KEEP
from grants import SCHEMA, GrantsUnavailableError, resolve_database_url
from usage import labels

logger = logging.getLogger("hindsight.gateway")

SWEEP_SECONDS = 30
# Any constant shared by every gateway: it serialises their sweeps.
_SWEEP_LOCK = 0x48534755  # "HSGU"
UNATTRIBUTED = "unattributed"

_CREATE = (
    CREATE_RETAINED
    + f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.llm_usage (
    bank_id text NOT NULL,
    client text NOT NULL,
    operation text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    calls bigint NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cached_tokens bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (bank_id, client, operation, provider, model)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.llm_usage_counted (
    id uuid PRIMARY KEY
);
"""
)

_KEYS = "bank_id, client, operation, provider, model"


def _sweep_sql(source: str) -> str:
    # The final SELECT returns the attributed calls of sampled traces, for their spans.
    return f"""
WITH new AS (
    SELECT r.id,
           coalesce(r.bank_id, '') AS bank_id,
           coalesce(d.client, '{UNATTRIBUTED}') AS client,
           coalesce(r.operation, '') AS operation,
           coalesce(r.provider, '') AS provider,
           coalesce(r.model, '') AS model,
           coalesce(r.input_tokens, 0) AS input_tokens,
           coalesce(r.output_tokens, 0) AS output_tokens,
           coalesce(r.cached_tokens, 0) AS cached_tokens,
           r.status, r.started_at, r.ended_at, d.trace_id, d.span_id, d.sampled
    FROM {source} r
    LEFT JOIN {SCHEMA}.retained_documents d
        ON d.bank_id = r.bank_id AND d.document_id = r.metadata->>'document_id'
    WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.llm_usage_counted c WHERE c.id = r.id)
), counted AS (
    INSERT INTO {SCHEMA}.llm_usage_counted (id) SELECT id FROM new
), totals AS (
    INSERT INTO {SCHEMA}.llm_usage AS u
        ({_KEYS}, calls, input_tokens, output_tokens, cached_tokens)
    SELECT {_KEYS}, count(*), sum(input_tokens), sum(output_tokens), sum(cached_tokens)
    FROM new GROUP BY {_KEYS}
    ON CONFLICT ({_KEYS}) DO UPDATE SET
        calls = u.calls + EXCLUDED.calls,
        input_tokens = u.input_tokens + EXCLUDED.input_tokens,
        output_tokens = u.output_tokens + EXCLUDED.output_tokens,
        cached_tokens = u.cached_tokens + EXCLUDED.cached_tokens
)
SELECT bank_id, client, operation, provider, model, input_tokens, output_tokens,
       cached_tokens, status, started_at, ended_at, trace_id, span_id
FROM new WHERE sampled ORDER BY started_at
"""


def _forget_sql(source: str) -> str:
    # A record Hindsight deleted never comes back, so its ID is no longer needed.
    return f"""
DELETE FROM {SCHEMA}.llm_usage_counted c
WHERE NOT EXISTS (SELECT 1 FROM {source} r WHERE r.id = c.id)
"""


_FORGET_RETAINED = f"""
DELETE FROM {SCHEMA}.retained_documents WHERE retained_at < now() - interval '{KEEP}'
"""


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Usage:
    """The running total of one bank, client, operation, provider and model."""

    bank: str
    client: str
    operation: str
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int


@dataclass(frozen=True)
class LlmCall:
    """One LLM call attributed to a sampled trace, for its span (FR-020)."""

    bank: str
    client: str
    operation: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    status: str
    started_at: datetime
    ended_at: datetime | None
    trace_id: str
    span_id: str


class LlmUsage:
    """The LLM usage totals in Hindsight's PostgreSQL. The pool opens on first use."""

    def __init__(self, database_url: str, hindsight_schema: str = "public"):
        self._database_url = database_url
        self._source = f"{_quote_identifier(hindsight_schema or 'public')}.llm_requests"
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
                    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
                    async with pool.acquire() as connection:
                        try:
                            await connection.execute(_CREATE)
                        except asyncpg.UniqueViolationError:
                            # Another unit created the tables at the same moment.
                            await connection.execute(_CREATE)
                except (OSError, asyncpg.PostgresError, GrantsUnavailableError) as exc:
                    raise GrantsUnavailableError(str(exc)) from exc
                self._pool = pool
        return self._pool

    async def sweep(self) -> list[LlmCall]:
        """Add the records not yet counted to the totals, in one transaction.

        Returns the calls just counted that belong to a sampled trace.
        """
        import asyncpg

        pool = await self._connected()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SWEEP_LOCK)
                rows = await connection.fetch(_sweep_sql(self._source))
                await connection.execute(_forget_sql(self._source))
                await connection.execute(_FORGET_RETAINED)
        except (OSError, asyncpg.PostgresError) as exc:
            raise GrantsUnavailableError(str(exc)) from exc
        return [LlmCall(*row) for row in rows]

    async def totals(self) -> list[Usage]:
        import asyncpg

        pool = await self._connected()
        try:
            rows = await pool.fetch(
                f"SELECT {_KEYS}, calls, input_tokens, output_tokens, cached_tokens"
                f" FROM {SCHEMA}.llm_usage ORDER BY {_KEYS}"
            )
        except (OSError, asyncpg.PostgresError) as exc:
            raise GrantsUnavailableError(str(exc)) from exc
        return [Usage(*row) for row in rows]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


async def sweep_forever(
    usage: LlmUsage,
    seconds: float = SWEEP_SECONDS,
    on_calls: Callable[[list[LlmCall]], None] | None = None,
) -> None:
    """Sweep until cancelled, passing attributed calls to ``on_calls``.

    A failed sweep is retried at the next one (FR-009).
    """
    while True:
        try:
            calls = await usage.sweep()
        except GrantsUnavailableError as exc:
            logger.warning("LLM usage not swept: %s", exc)
        else:
            if calls and on_calls is not None:
                try:
                    on_calls(calls)
                except Exception:  # the totals are kept even if the spans fail
                    logger.exception("spans of LLM calls not recorded")
        await asyncio.sleep(seconds)


# ---- Prices ------------------------------------------------------------------

PRICE_FILE = "model_prices_and_context_window_backup.json"


def bundled_price_file() -> Path | None:
    """The price list of the image's LiteLLM, found without importing LiteLLM."""
    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(next(iter(spec.submodule_search_locations))) / PRICE_FILE
    return path if path.is_file() else None


def load_prices(path: Path | None) -> dict[str, dict]:
    if path is None:
        logger.warning("no LiteLLM price list: LLM cost is not reported")
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("cannot read the LiteLLM price list %s: %s", path, exc)
        return {}


def cost(prices: dict[str, dict], usage: Usage) -> float | None:
    """The cost in US dollars, or None if the price list does not name the model."""
    entry = prices.get(f"{usage.provider}/{usage.model}") or prices.get(usage.model)
    if not isinstance(entry, dict):
        return None
    input_price = entry.get("input_cost_per_token")
    output_price = entry.get("output_cost_per_token")
    if input_price is None or output_price is None:
        return None
    cached_price = entry.get("cache_read_input_token_cost")
    if cached_price is None:
        cached_price = input_price
    cached = min(usage.cached_tokens, usage.input_tokens)
    return (
        (usage.input_tokens - cached) * input_price
        + cached * cached_price
        + usage.output_tokens * output_price
    )


def lines(totals: list[Usage] | None, prices: dict[str, dict]) -> list[str]:
    """The totals in the Prometheus text format; ``None`` means they cannot be read."""
    out = [
        "# HELP hindsight_gateway_llm_usage_up Whether the LLM usage totals could be read.",
        "# TYPE hindsight_gateway_llm_usage_up gauge",
        f"hindsight_gateway_llm_usage_up {0 if totals is None else 1}",
    ]
    if not totals:
        return out
    calls = [
        "# HELP hindsight_gateway_llm_calls_total LLM calls Hindsight made, per bank and client.",
        "# TYPE hindsight_gateway_llm_calls_total counter",
    ]
    tokens = [
        "# HELP hindsight_gateway_llm_tokens_total LLM tokens, per bank and client."
        " Input includes cached.",
        "# TYPE hindsight_gateway_llm_tokens_total counter",
    ]
    costs = [
        "# HELP hindsight_gateway_llm_cost_usd_total LLM cost in US dollars, per bank and client.",
        "# TYPE hindsight_gateway_llm_cost_usd_total counter",
    ]
    for usage in totals:
        base = {
            "bank": usage.bank,
            "client": usage.client,
            "operation": usage.operation,
            "provider": usage.provider,
            "model": usage.model,
        }
        calls.append(f"hindsight_gateway_llm_calls_total{labels(**base)} {usage.calls}")
        for kind, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cached", usage.cached_tokens),
        ):
            label_set = labels(**base, type=kind)
            tokens.append(f"hindsight_gateway_llm_tokens_total{label_set} {count}")
        dollars = cost(prices, usage)
        if dollars is not None:
            costs.append(f"hindsight_gateway_llm_cost_usd_total{labels(**base)} {dollars:.10g}")
    return out + calls + tokens + (costs if len(costs) > 2 else [])
