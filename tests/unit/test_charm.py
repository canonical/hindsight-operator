# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""State-transition (Scenario) tests for the hindsight charm."""

import dataclasses
from pathlib import Path

import pytest
from ops import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus, pebble, testing

import charm as charm_module
import workload
from charm import HindsightCharm
from defaults import (
    API_READY_CHECK,
    API_SERVICE,
    CONTAINER,
    CONTROL_PLANE_CHECK,
    CONTROL_PLANE_SERVICE,
    PG0_DATA_DIR,
)

DB_DATA = {
    "username": "relation-3",
    "password": "s3cret",
    "endpoints": "postgresql-k8s-primary.model.svc.cluster.local:5432",
    "database": "hindsight",
}


@pytest.fixture(autouse=True)
def api_up(monkeypatch):
    """Stub the direct /health probe; tests flip it via the returned dict."""
    state = {"up": True}
    monkeypatch.setattr(charm_module, "api_responding", lambda **_: state["up"])
    return state


@pytest.fixture
def ctx():
    return testing.Context(HindsightCharm)


@pytest.fixture
def pg0_mount(tmp_path: Path):
    return testing.Mount(location=PG0_DATA_DIR, source=tmp_path)


def _state(**kwargs):
    """A State whose unit has the `data` volume, as when deployed with --storage."""
    kwargs.setdefault("storages", {testing.Storage("data")})
    return testing.State(**kwargs)


def _container(pg0_mount, **kwargs):
    kwargs.setdefault("can_connect", True)
    kwargs.setdefault("execs", {testing.Exec(["chown"], return_code=0)})
    return testing.Container(CONTAINER, mounts={"data": pg0_mount}, **kwargs)


def _running_container(pg0_mount, api_status=pebble.CheckStatus.UP, cp_enabled=True):
    """A container whose Hindsight layer is already planned and checked."""
    layer = workload.build_layer({}, {}, cp_enabled)
    return _container(
        pg0_mount,
        layers={CONTAINER: pebble.Layer(layer)},
        service_statuses={
            API_SERVICE: pebble.ServiceStatus.ACTIVE,
            CONTROL_PLANE_SERVICE: pebble.ServiceStatus.ACTIVE
            if cp_enabled
            else pebble.ServiceStatus.INACTIVE,
        },
        check_infos={
            testing.CheckInfo(
                API_READY_CHECK,
                level=pebble.CheckLevel.READY,
                status=api_status,
                successes=1 if api_status == pebble.CheckStatus.UP else 0,
            ),
            testing.CheckInfo(CONTROL_PLANE_CHECK, status=pebble.CheckStatus.UP),
        },
    )


def _api_env(state_out):
    return state_out.get_container(CONTAINER).plan.services[API_SERVICE].environment


def test_pebble_not_ready_waits(ctx, pg0_mount):
    container = _container(pg0_mount, can_connect=False)
    out = ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))
    assert out.unit_status == WaitingStatus("waiting for Pebble in workload container")


def test_model_proxy_reaches_the_api_only(ctx, pg0_mount, monkeypatch):
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://egress:3128")
    monkeypatch.setenv("JUJU_CHARM_NO_PROXY", "localhost,.svc")
    container = _container(pg0_mount)
    state = _state(containers={container}, leader=True)

    out = ctx.run(ctx.on.pebble_ready(container), state)

    services = out.get_container(CONTAINER).plan.services
    api = services[API_SERVICE].environment
    assert api["HTTPS_PROXY"] == api["https_proxy"] == "http://egress:3128"
    assert api["NO_PROXY"] == api["no_proxy"] == "localhost,.svc"
    # The Control Plane only talks to the API on localhost.
    assert "HTTPS_PROXY" not in services[CONTROL_PLANE_SERVICE].environment


def test_update_status_applies_a_changed_model_proxy(ctx, pg0_mount, monkeypatch):
    container = _container(pg0_mount)
    state = _state(containers={container}, leader=True)
    state = ctx.run(ctx.on.pebble_ready(container), state)
    assert "HTTPS_PROXY" not in _api_env(state)

    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://egress:3128")
    state = ctx.run(ctx.on.update_status(), state)
    assert _api_env(state)["HTTPS_PROXY"] == "http://egress:3128"

    monkeypatch.delenv("JUJU_CHARM_HTTPS_PROXY")
    state = ctx.run(ctx.on.update_status(), state)
    assert "HTTPS_PROXY" not in _api_env(state)
    assert "https_proxy" not in _api_env(state)


def test_update_status_leaves_services_alone_when_proxy_is_unchanged(ctx, pg0_mount, monkeypatch):
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://egress:3128")
    container = _container(pg0_mount)
    state = _state(containers={container}, leader=True)
    state = ctx.run(ctx.on.pebble_ready(container), state)

    calls = []
    monkeypatch.setattr(
        charm_module.HindsightCharm, "_reconcile", lambda self, event: calls.append(event)
    )
    ctx.run(ctx.on.update_status(), state)
    assert calls == []


def test_embedded_mode_plans_both_services(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, leader=True)

    out = ctx.run(ctx.on.pebble_ready(container), state)

    plan = out.get_container(CONTAINER).plan
    assert set(plan.services) == {API_SERVICE, CONTROL_PLANE_SERVICE}
    api = plan.services[API_SERVICE]
    assert api.user == "hindsight"
    assert api.command == "/app/api/.venv/bin/hindsight-api"
    assert api.environment["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert "HINDSIGHT_API_DATABASE_URL" not in api.environment
    assert plan.services[CONTROL_PLANE_SERVICE].startup == "enabled"
    assert {p.port for p in out.opened_ports} == {8888, 9999}


def test_embedded_mode_chowns_root_owned_pg0_mount(ctx, pg0_mount):
    container = _container(pg0_mount)
    ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))

    commands = [e.command for e in ctx.exec_history[CONTAINER]]
    assert ["chown", "-R", "hindsight:hindsight", PG0_DATA_DIR] in commands


def test_failed_chown_blocks_and_does_not_start(ctx, pg0_mount):
    container = _container(
        pg0_mount, execs={testing.Exec(["chown"], return_code=1, stderr="EPERM")}
    )
    out = ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))
    assert out.get_container(CONTAINER).plan.services == {}
    assert isinstance(out.unit_status, BlockedStatus)
    assert "cannot make" in out.unit_status.message


def test_active_when_api_check_up(ctx, pg0_mount):
    container = _running_container(pg0_mount)
    out = ctx.run(ctx.on.update_status(), _state(containers={container}))
    assert out.unit_status == ActiveStatus("embedded database")


def test_waiting_when_api_check_down(ctx, pg0_mount):
    container = _running_container(pg0_mount, api_status=pebble.CheckStatus.DOWN)
    out = ctx.run(ctx.on.update_status(), _state(containers={container}))
    assert out.unit_status == WaitingStatus("waiting for Hindsight API to become ready")


def test_waiting_when_api_check_up_but_never_succeeded(ctx, pg0_mount):
    # Pebble reports a new check as "up" before its first run; don't trust it.
    container = _running_container(pg0_mount)
    fresh = testing.CheckInfo(
        API_READY_CHECK, level=pebble.CheckLevel.READY, status=pebble.CheckStatus.UP, successes=0
    )
    others = {c for c in container.check_infos if c.name != API_READY_CHECK}
    container = dataclasses.replace(container, check_infos=others | {fresh})
    out = ctx.run(ctx.on.update_status(), _state(containers={container}))
    assert out.unit_status == WaitingStatus("waiting for Hindsight API to become ready")


def test_waiting_when_check_stale_but_api_not_answering(ctx, pg0_mount, api_up):
    # Right after a replan the check can still read "up" from before the restart.
    api_up["up"] = False
    container = _running_container(pg0_mount)
    out = ctx.run(ctx.on.update_status(), _state(containers={container}))
    assert out.unit_status == WaitingStatus("waiting for Hindsight API to become ready")


def test_check_failure_event_refreshes_status(ctx, pg0_mount):
    container = _running_container(pg0_mount, api_status=pebble.CheckStatus.DOWN)
    info = next(c for c in container.check_infos if c.name == API_READY_CHECK)
    out = ctx.run(ctx.on.pebble_check_failed(container, info), _state(containers={container}))
    assert out.unit_status == WaitingStatus("waiting for Hindsight API to become ready")


def test_maintenance_before_layer_is_planned(ctx, pg0_mount):
    # update-status does not reconcile, so nothing gets planned here.
    container = _container(pg0_mount)
    out = ctx.run(ctx.on.update_status(), _state(containers={container}))
    assert out.unit_status == MaintenanceStatus("configuring Hindsight")


def test_llm_config_flows_into_api_env(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(
        containers={container},
        config={
            "llm-provider": "ollama",
            "llm-model": "gemma3:12b",
            "llm-base-url": "http://ollama.ollama.svc:11434/v1",
            "extra-env": "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai",
        },
    )
    env = _api_env(ctx.run(ctx.on.config_changed(), state))
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "ollama"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "gemma3:12b"
    assert env["HINDSIGHT_API_LLM_BASE_URL"] == "http://ollama.ollama.svc:11434/v1"
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openai"
    assert env["HINDSIGHT_API_WORKER_ID"] == "hindsight-0"


def test_env_secret_is_merged(ctx, pg0_mount):
    secret = testing.Secret(tracked_content={"llm-api-key": "sk-test", "tenant-api-key": "hs-key"})
    container = _container(pg0_mount)
    state = _state(containers={container}, secrets={secret}, config={"env-secret": secret.id})
    out = ctx.run(ctx.on.config_changed(), state)

    services = out.get_container(CONTAINER).plan.services
    api = services[API_SERVICE].environment
    assert api["HINDSIGHT_API_LLM_API_KEY"] == "sk-test"
    assert api["HINDSIGHT_API_TENANT_API_KEY"] == "hs-key"
    assert api["HINDSIGHT_API_TENANT_EXTENSION"].endswith(":ApiKeyTenantExtension")
    cp = services[CONTROL_PLANE_SERVICE].environment
    assert cp["HINDSIGHT_CP_DATAPLANE_API_KEY"] == "hs-key"


def test_unreadable_secret_blocks(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, config={"env-secret": "secret:cvh7kruupa1s46bqvuig"})
    out = ctx.run(ctx.on.config_changed(), state)
    assert out.get_container(CONTAINER).plan.services == {}
    assert out.unit_status == BlockedStatus(
        "cannot read env-secret: run `juju grant-secret <secret> hindsight`"
    )


def test_control_plane_can_be_disabled(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, config={"control-plane-enabled": False})
    out = ctx.run(ctx.on.config_changed(), state)

    cp = out.get_container(CONTAINER).plan.services[CONTROL_PLANE_SERVICE]
    assert cp.startup == "disabled"
    assert {p.port for p in out.opened_ports} == {8888}


def test_disabling_control_plane_stops_running_service(ctx, pg0_mount):
    container = _running_container(pg0_mount, cp_enabled=True)
    state = _state(containers={container}, config={"control-plane-enabled": False})
    out = ctx.run(ctx.on.config_changed(), state)
    statuses = out.get_container(CONTAINER).service_statuses
    assert statuses[CONTROL_PLANE_SERVICE] == pebble.ServiceStatus.INACTIVE
    assert statuses[API_SERVICE] == pebble.ServiceStatus.ACTIVE


def test_embedded_disallowed_without_relation_blocks(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, config={"allow-embedded-database": False})
    out = ctx.run(ctx.on.config_changed(), state)
    assert out.get_container(CONTAINER).plan.services == {}
    assert out.unit_status == BlockedStatus(
        "no database: relate postgresql (allow-embedded-database is off)"
    )


def test_embedded_mode_refuses_to_scale(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, planned_units=2)
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, BlockedStatus)
    assert "embedded database supports one unit" in out.unit_status.message


def test_database_relation_without_credentials_waits(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation("database", remote_app_name="postgresql-k8s")
    state = _state(containers={container}, relations={relation}, leader=True)
    out = ctx.run(ctx.on.relation_changed(relation), state)
    assert out.get_container(CONTAINER).plan.services == {}
    assert out.unit_status == WaitingStatus("waiting for credentials over the database relation")


def test_database_relation_sets_url_and_allows_scaling(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation(
        "database", remote_app_name="postgresql-k8s", remote_app_data=DB_DATA
    )
    state = _state(containers={container}, relations={relation}, leader=True, planned_units=3)
    out = ctx.run(ctx.on.relation_changed(relation), state)

    env = _api_env(out)
    assert env["HINDSIGHT_API_DATABASE_URL"] == (
        "postgresql://relation-3:s3cret@"
        "postgresql-k8s-primary.model.svc.cluster.local:5432/hindsight"
    )
    # No chown: the pg0 volume is irrelevant with an external database.
    assert ctx.exec_history.get(CONTAINER, []) == []
    assert not isinstance(out.unit_status, BlockedStatus)


def test_database_relation_requests_hindsight_database(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation("database", remote_app_name="postgresql-k8s")
    state = _state(containers={container}, relations={relation}, leader=True)
    out = ctx.run(ctx.on.relation_created(relation), state)
    assert out.get_relation(relation.id).local_app_data["database"] == "hindsight"


def test_active_with_external_database_has_no_note(ctx, pg0_mount):
    container = _running_container(pg0_mount)
    relation = testing.Relation(
        "database", remote_app_name="postgresql-k8s", remote_app_data=DB_DATA
    )
    state = _state(containers={container}, relations={relation}, leader=True)
    out = ctx.run(ctx.on.update_status(), state)
    assert out.unit_status == ActiveStatus("")


def test_ingress_requests_api_port(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation("ingress", remote_app_name="traefik-k8s")
    state = _state(containers={container}, relations={relation}, leader=True)
    out = ctx.run(ctx.on.relation_changed(relation, remote_unit=0), state)
    data = out.get_relation(relation.id).local_app_data
    assert data["port"] == "8888"
    assert data["strip-prefix"] == "true"


def _stored(**content):
    return testing.StoredState("_stored", owner_path="HindsightCharm", content=content)


def _stored_out(state_out):
    return state_out.get_stored_state("_stored", owner_path="HindsightCharm").content


def test_embedded_unit_with_data_keeps_serving_when_scaled(ctx, pg0_mount, tmp_path):
    # The unit whose volume holds the memories stays up; only new units block.
    (tmp_path / "data").mkdir()
    container = _running_container(pg0_mount)
    state = _state(containers={container}, planned_units=2)

    out = ctx.run(ctx.on.config_changed(), state)

    statuses = out.get_container(CONTAINER).service_statuses
    assert statuses[API_SERVICE] == pebble.ServiceStatus.ACTIVE
    assert out.unit_status == ActiveStatus("embedded database; other units blocked")


def test_embedded_unit_ignores_lost_and_found_when_scaled(ctx, pg0_mount, tmp_path):
    (tmp_path / "lost+found").mkdir()
    container = _container(pg0_mount)
    out = ctx.run(ctx.on.config_changed(), _state(containers={container}, planned_units=2))
    assert "embedded database supports one unit" in out.unit_status.message


def test_volume_error_survives_later_hooks(ctx, pg0_mount):
    # update-status does not reconcile, so it must still report the cause.
    container = _running_container(pg0_mount)
    state = _state(containers={container}, stored_states={_stored(volume_error=True)})
    out = ctx.run(ctx.on.update_status(), state)
    assert out.unit_status == BlockedStatus(
        f"cannot make {PG0_DATA_DIR} writable; see juju debug-log"
    )


def test_failed_chown_is_remembered(ctx, pg0_mount):
    container = _container(
        pg0_mount, execs={testing.Exec(["chown"], return_code=1, stderr="EPERM")}
    )
    out = ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))
    assert _stored_out(out)["volume_error"] is True


def test_successful_reconcile_clears_volume_error(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, stored_states={_stored(volume_error=True)})
    out = ctx.run(ctx.on.pebble_ready(container), state)
    assert _stored_out(out)["volume_error"] is False
    assert not isinstance(out.unit_status, BlockedStatus)


def test_pebble_api_error_during_chown_blocks(ctx, pg0_mount, monkeypatch):
    def fail(*_args, **_kwargs):
        raise pebble.APIError({}, 500, "Internal Server Error", "exec failed")

    monkeypatch.setattr(charm_module.ops.Container, "exec", fail)
    container = _container(pg0_mount)
    out = ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))
    assert out.get_container(CONTAINER).plan.services == {}
    assert out.unit_status == BlockedStatus(
        f"cannot make {PG0_DATA_DIR} writable; see juju debug-log"
    )


def test_missing_pg0_dir_is_created(ctx):
    # No mount: the data directory does not exist yet in the container.
    container = testing.Container(CONTAINER, can_connect=True)
    out = ctx.run(ctx.on.pebble_ready(container), _state(containers={container}))
    assert API_SERVICE in out.get_container(CONTAINER).plan.services


def test_using_relation_database_is_remembered(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation(
        "database", remote_app_name="postgresql-k8s", remote_app_data=DB_DATA
    )
    state = _state(containers={container}, relations={relation}, leader=True)
    out = ctx.run(ctx.on.config_changed(), state)
    assert _stored_out(out)["external_database_used"] is True


def test_removing_database_relation_does_not_fall_back_to_empty_pg0(ctx, pg0_mount):
    container = _running_container(pg0_mount)
    relation = testing.Relation(
        "database", remote_app_name="postgresql-k8s", remote_app_data=DB_DATA
    )
    state = _state(
        containers={container},
        relations={relation},
        leader=True,
        stored_states={_stored(external_database_used=True)},
    )

    out = ctx.run(ctx.on.relation_broken(relation), state)

    statuses = out.get_container(CONTAINER).service_statuses
    assert statuses[API_SERVICE] == pebble.ServiceStatus.INACTIVE
    assert out.unit_status == BlockedStatus(
        "database relation removed: relate postgresql again, or run use-embedded-database"
    )


def test_use_embedded_database_action_opts_back_in(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, stored_states={_stored(external_database_used=True)})

    out = ctx.run(ctx.on.action("use-embedded-database"), state)

    assert _stored_out(out)["external_database_used"] is False
    env = _api_env(out)
    assert "HINDSIGHT_API_DATABASE_URL" not in env
    assert ctx.action_results == {"result": "switched to the embedded database"}


def test_use_embedded_database_action_refuses_while_related(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation(
        "database", remote_app_name="postgresql-k8s", remote_app_data=DB_DATA
    )
    state = _state(containers={container}, relations={relation}, leader=True)
    with pytest.raises(testing.ActionFailed, match="remove it first"):
        ctx.run(ctx.on.action("use-embedded-database"), state)


def test_embedded_mode_without_data_storage_blocks_and_starts_nothing(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, storages=set(), leader=True)

    out = ctx.run(ctx.on.pebble_ready(container), state)

    assert out.unit_status == BlockedStatus(charm_module._NO_STORAGE_MESSAGE)
    assert API_SERVICE not in out.get_container(CONTAINER).plan.services


def test_external_database_needs_no_data_storage(ctx, pg0_mount):
    container = _container(pg0_mount)
    relation = testing.Relation("database", remote_app_data=DB_DATA)
    state = _state(containers={container}, relations={relation}, storages=set(), leader=True)

    out = ctx.run(ctx.on.relation_changed(relation), state)

    env = out.get_container(CONTAINER).plan.services[API_SERVICE].environment
    assert env["HINDSIGHT_API_DATABASE_URL"].startswith("postgresql://")
    assert out.unit_status == ActiveStatus("")


def test_use_embedded_database_action_refuses_without_data_storage(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(containers={container}, storages=set(), leader=True)

    with pytest.raises(testing.ActionFailed) as exc:
        ctx.run(ctx.on.action("use-embedded-database"), state)

    assert "no data storage" in exc.value.message


def test_crash_looping_api_says_where_to_look(ctx, pg0_mount, api_up):
    api_up["up"] = False
    container = dataclasses.replace(
        _running_container(pg0_mount, api_status=pebble.CheckStatus.DOWN),
        service_statuses={
            API_SERVICE: pebble.ServiceStatus.ERROR,
            CONTROL_PLANE_SERVICE: pebble.ServiceStatus.ACTIVE,
        },
    )

    out = ctx.run(ctx.on.update_status(), _state(containers={container}))

    assert out.unit_status.name == "waiting"
    assert "pebble logs hindsight-api" in out.unit_status.message
