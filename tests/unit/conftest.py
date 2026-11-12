# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""A real PostgreSQL for the gateway's LLM usage tests (003), from pg0."""

import asyncio
import uuid

import pytest

# The columns of Hindsight 0.9.2's ``llm_requests`` the gateway reads.
_LLM_REQUESTS = """
CREATE TABLE IF NOT EXISTS llm_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_id text, operation text, provider text, model text,
    status text NOT NULL DEFAULT 'success',
    started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
    input_tokens integer, output_tokens integer, cached_tokens integer,
    metadata jsonb DEFAULT '{}'::jsonb
)
"""


@pytest.fixture(scope="module")
def database_url():
    pg0 = pytest.importorskip("pg0")
    instance = pg0.Pg0(name=f"gateway-usage-{uuid.uuid4().hex[:8]}")
    try:
        info = instance.start()
    except Exception as exc:  # no bundled PostgreSQL for this platform
        pytest.skip(f"pg0 cannot start PostgreSQL here: {exc}")
    yield info.uri
    instance.drop()


@pytest.fixture
def database(database_url):
    import asyncpg

    async def reset():
        connection = await asyncpg.connect(database_url)
        try:
            await connection.execute(
                "DROP SCHEMA IF EXISTS hindsight_gateway CASCADE;"
                " DROP TABLE IF EXISTS llm_requests;"
            )
            await connection.execute(_LLM_REQUESTS)
        finally:
            await connection.close()

    asyncio.run(reset())
    return database_url
