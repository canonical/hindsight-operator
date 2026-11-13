# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Which emails each bank is granted to (002 FR-007 to FR-011).

Grants are one table in the `hindsight_gateway` schema of Hindsight's own
database, so every unit shares them and they are kept across restarts. The
gateway never touches Hindsight's tables (architecture.md§9).

With the embedded database, the address comes from pg0 once the API has
started it. pg0 keeps its state under the home directory the API and the
gateway share.

This module runs inside the Hindsight image (Python 3.11), on its asyncpg.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

logger = logging.getLogger("hindsight.gateway")

SCHEMA = "hindsight_gateway"
_CREATE = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.bank_grants (
    bank_id text NOT NULL,
    email text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bank_id, email)
);
CREATE INDEX IF NOT EXISTS bank_grants_email ON {SCHEMA}.bank_grants (email);
"""


class GrantsUnavailableError(Exception):
    """The grants cannot be read or changed now."""


class MemoryGrants:
    """Grants held in memory, for tests."""

    def __init__(self):
        self.rows: dict[str, set[str]] = {}

    async def banks_for(self, email: str) -> frozenset[str]:
        return frozenset(bank for bank, emails in self.rows.items() if email in emails)

    async def emails(self, bank: str) -> list[str]:
        return sorted(self.rows.get(bank, ()))

    async def grant(self, bank: str, email: str) -> None:
        self.rows.setdefault(bank, set()).add(email)

    async def withdraw(self, bank: str, email: str) -> bool:
        emails = self.rows.get(bank, set())
        if email not in emails:
            return False
        emails.discard(email)
        return True

    async def close(self) -> None:
        pass


class PostgresGrants:
    """Grants in Hindsight's PostgreSQL. The pool opens on first use."""

    def __init__(self, database_url: str):
        self._database_url = database_url
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
                            await connection.execute(_CREATE)
                        except asyncpg.UniqueViolationError:
                            # Another unit created the schema at the same moment.
                            await connection.execute(_CREATE)
                except (OSError, asyncpg.PostgresError, GrantsUnavailableError) as exc:
                    logger.warning("grants database unavailable: %s", exc)
                    raise GrantsUnavailableError(str(exc)) from exc
                self._pool = pool
        return self._pool

    async def _run(self, query: str, *args, fetch: bool = False):
        import asyncpg

        pool = await self._connected()
        try:
            if fetch:
                return await pool.fetch(query, *args)
            return await pool.execute(query, *args)
        except (OSError, asyncpg.PostgresError) as exc:
            logger.warning("grants database error: %s", exc)
            raise GrantsUnavailableError(str(exc)) from exc

    async def banks_for(self, email: str) -> frozenset[str]:
        rows = await self._run(
            f"SELECT bank_id FROM {SCHEMA}.bank_grants WHERE email = $1", email, fetch=True
        )
        return frozenset(row["bank_id"] for row in rows)

    async def emails(self, bank: str) -> list[str]:
        rows = await self._run(
            f"SELECT email FROM {SCHEMA}.bank_grants WHERE bank_id = $1 ORDER BY email",
            bank,
            fetch=True,
        )
        return [row["email"] for row in rows]

    async def grant(self, bank: str, email: str) -> None:
        await self._run(
            f"INSERT INTO {SCHEMA}.bank_grants (bank_id, email) VALUES ($1, $2)"
            " ON CONFLICT DO NOTHING",
            bank,
            email,
        )

    async def withdraw(self, bank: str, email: str) -> bool:
        status = await self._run(
            f"DELETE FROM {SCHEMA}.bank_grants WHERE bank_id = $1 AND email = $2", bank, email
        )
        return status != "DELETE 0"

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


def resolve_database_url(url: str) -> str:
    """A PostgreSQL URL for the API's ``HINDSIGHT_API_DATABASE_URL``.

    An empty URL, ``pg0`` or ``pg0://[user[:password]@]name[:port]`` names
    Hindsight's embedded database: ask pg0 for the address of the running
    instance. Raises :class:`GrantsUnavailableError` until the API has started it.
    """
    if url and not url.startswith("pg0"):
        return url
    name = "hindsight"
    if url.startswith("pg0://"):
        host = urlsplit(url).hostname
        name = host or name
    try:
        import pg0

        info = pg0.info(name)
    except Exception as exc:  # pg0 reports a stopped or missing instance as an error
        raise GrantsUnavailableError(f"the embedded database is not running: {exc}") from exc
    if not info.running or not info.uri:
        raise GrantsUnavailableError("the embedded database is not running yet")
    return info.uri
