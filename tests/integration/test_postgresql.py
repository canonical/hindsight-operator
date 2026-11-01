# Copyright 2026 Matias Piipari
# See LICENSE file for licensing details.
"""Integration tests for hindsight backed by postgresql-k8s (pgvector)."""

from __future__ import annotations

import jubilant
import pytest

from conftest import APP
from helpers import api_environment, http, retain_and_recall, wait_active, workload

POSTGRES = "postgresql-k8s"
POSTGRES_CHANNEL = "14/stable"


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    juju.deploy(
        POSTGRES,
        channel=POSTGRES_CHANNEL,
        trust=True,
        # Hindsight requires pgvector; the relation user cannot CREATE EXTENSION.
        config={"plugin_vector_enable": True},
    )
    juju.deploy(charm, APP, resources=resources, config={"allow-embedded-database": False})
    # Without a database (and embedded mode disallowed) hindsight blocks.
    juju.wait(lambda s: jubilant.all_blocked(s, APP))

    juju.integrate(APP, POSTGRES)
    wait_active(juju)


def test_status_has_no_embedded_note(juju: jubilant.Juju):
    status = juju.status()
    assert status.apps[APP].units[f"{APP}/0"].workload_status.message == ""


def test_database_url_points_at_postgresql(juju: jubilant.Juju):
    url = api_environment(juju, f"{APP}/0")["HINDSIGHT_API_DATABASE_URL"]
    assert url.startswith("postgresql://")
    assert url.endswith("/hindsight")
    assert POSTGRES in url


def test_pg0_is_not_used(juju: jubilant.Juju):
    assert not workload(juju, f"{APP}/0", "ls -A /home/hindsight/.pg0").strip()


def test_retain_and_recall(juju: jubilant.Juju):
    retain_and_recall(juju, f"{APP}/0", bank="postgres-it")


def test_scale_out_shares_memory(juju: jubilant.Juju):
    juju.add_unit(APP)
    juju.wait(
        lambda s: len(s.apps[APP].units) == 2 and jubilant.all_active(s),
        error=jubilant.any_error,
    )
    # Memory retained via unit 0 must be visible from unit 1 (same database).
    recalled = http(
        juju,
        f"{APP}/1",
        "POST",
        "/v1/default/banks/postgres-it/memories/recall",
        {"query": "zebras"},
    )
    assert recalled.status == 200, recalled.body
    assert "zebras" in recalled.body, recalled.body

    workers = {
        api_environment(juju, unit)["HINDSIGHT_API_WORKER_ID"] for unit in (f"{APP}/0", f"{APP}/1")
    }
    assert workers == {f"{APP}-0", f"{APP}-1"}


def test_removing_relation_blocks_instead_of_using_empty_pg0(juju: jubilant.Juju):
    juju.remove_unit(APP, num_units=1)
    juju.wait(lambda s: len(s.apps[APP].units) == 1, error=jubilant.any_error)
    # Allow embedded mode, so the only thing keeping the unit off pg0 is the
    # memory of having used the external database.
    juju.config(APP, {"allow-embedded-database": True})
    juju.remove_relation(APP, POSTGRES)

    def removed(status: jubilant.Status) -> bool:
        unit = status.apps[APP].units[f"{APP}/0"]
        return unit.workload_status.message.startswith("database relation removed")

    juju.wait(removed, error=jubilant.any_error)
    assert juju.status().apps[APP].units[f"{APP}/0"].workload_status.current == "blocked"


def test_use_embedded_database_action(juju: jubilant.Juju):
    task = juju.run(f"{APP}/0", "use-embedded-database")
    assert task.results["result"] == "switched to the embedded database"
    wait_active(juju)
    status = juju.status().apps[APP].units[f"{APP}/0"].workload_status
    assert status.message == "embedded database"
    assert "HINDSIGHT_API_DATABASE_URL" not in api_environment(juju, f"{APP}/0")
