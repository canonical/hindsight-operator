# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Integration tests for hindsight with its embedded pg0 database.

This is the Kubernetes analogue of the Workshop SDK's default setup: no
external database, memory persisted on the `data` storage volume.
"""

from __future__ import annotations

import jubilant
import pytest

from conftest import APP
from helpers import api_environment, http, retain_and_recall, wait_active, workload

UNIT = f"{APP}/0"


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    juju.deploy(charm, APP, resources=resources)
    wait_active(juju)


def test_status_reports_embedded_database(juju: jubilant.Juju):
    status = juju.status()
    assert status.apps[APP].units[UNIT].workload_status.message == "embedded database"


def test_api_health(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/health")
    assert response.status == 200, response.body


def test_control_plane_serves(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/", port=9999)
    assert response.status < 500, response.body


def test_pg0_lives_on_storage_owned_by_workload_user(juju: jubilant.Juju):
    assert workload(juju, UNIT, "stat -c %U /home/hindsight/.pg0").strip() == "hindsight"
    assert workload(juju, UNIT, "ls -A /home/hindsight/.pg0").strip(), "pg0 dir is empty"


def test_retain_and_recall(juju: jubilant.Juju):
    retain_and_recall(juju, UNIT, bank="embedded-it")


def test_memory_survives_pod_restart(juju: jubilant.Juju):
    # Kill the workload container's PID 1 (Pebble) so Kubernetes restarts it.
    workload(juju, UNIT, "kill 1")
    wait_active(juju)
    recalled = http(
        juju, UNIT, "POST", "/v1/default/banks/embedded-it/memories/recall", {"query": "zebras"}
    )
    assert recalled.status == 200, recalled.body
    assert "zebras" in recalled.body, recalled.body


def test_config_reaches_pebble_plan(juju: jubilant.Juju):
    juju.config(APP, {"llm-model": "it-model", "extra-env": "HINDSIGHT_API_LOG_FORMAT=json"})
    wait_active(juju)
    env = api_environment(juju, UNIT)
    assert env["HINDSIGHT_API_LLM_MODEL"] == "it-model"
    assert env["HINDSIGHT_API_LOG_FORMAT"] == "json"
    juju.config(APP, reset=["llm-model", "extra-env"])
    wait_active(juju)


def test_tenant_api_key_from_secret_enforces_auth(juju: jubilant.Juju):
    uri = juju.add_secret("hindsight-it-keys", {"tenant-api-key": "it-token"})
    juju.grant_secret(uri, APP)
    juju.config(APP, {"env-secret": uri})
    wait_active(juju)

    bank = "/v1/default/banks/auth-it/memories/recall"
    assert http(juju, UNIT, "POST", bank, {"query": "x"}).status == 401
    assert http(juju, UNIT, "POST", bank, {"query": "x"}, token="it-token").status == 200

    juju.config(APP, reset=["env-secret"])
    wait_active(juju)
    assert http(juju, UNIT, "POST", bank, {"query": "x"}).status == 200


def test_disabling_control_plane_closes_its_port(juju: jubilant.Juju):
    juju.config(APP, {"control-plane-enabled": False})
    wait_active(juju)
    # `juju status` does not list open ports for K8s units; ask the hook tool.
    ports = juju.exec("opened-ports", unit=UNIT).stdout.split()
    assert "9999/tcp" not in ports
    assert "8888/tcp" in ports
    juju.config(APP, reset=["control-plane-enabled"])
    wait_active(juju)


def test_scaling_embedded_database_blocks_only_new_units(juju: jubilant.Juju):
    juju.add_unit(APP)

    def settled(status: jubilant.Status) -> bool:
        units = status.apps[APP].units
        return (
            len(units) == 2
            and units[UNIT].workload_status.current == "active"
            and units[f"{APP}/1"].workload_status.current == "blocked"
        )

    juju.wait(settled, error=jubilant.any_error)
    units = juju.status().apps[APP].units
    # Unit 0 holds the memories and keeps serving them.
    assert units[UNIT].workload_status.message == "embedded database; other units blocked"
    assert "embedded database supports one unit" in units[f"{APP}/1"].workload_status.message
    assert http(juju, UNIT, "GET", "/health").status == 200

    juju.remove_unit(APP, num_units=1)
    juju.wait(lambda s: len(s.apps[APP].units) == 1 and jubilant.all_active(s))
