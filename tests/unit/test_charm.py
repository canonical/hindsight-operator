# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""State-transition (Scenario) tests for the hindsight charm."""

import dataclasses
import json
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
    GATEWAY_DIR,
    GATEWAY_SERVICE,
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
    assert set(plan.services) == {API_SERVICE, CONTROL_PLANE_SERVICE, GATEWAY_SERVICE}
    assert plan.services[GATEWAY_SERVICE].startup == "disabled"
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


def _cp_ingress_state(pg0_mount, secret_content, *, cp_enabled=True, app_data=None):
    secret = testing.Secret(tracked_content=secret_content)
    relation = testing.Relation(
        "control-plane-ingress", local_app_data=app_data or {}, remote_app_name="cp-ingress"
    )
    state = _state(
        containers={_container(pg0_mount)},
        secrets={secret},
        relations={relation},
        leader=True,
        config={"env-secret": secret.id, "control-plane-enabled": cp_enabled},
    )
    return state, relation


@pytest.mark.parametrize("event", ["relation_joined", "relation_changed"])
def test_control_plane_ingress_published_with_access_key(ctx, pg0_mount, event):
    state, relation = _cp_ingress_state(pg0_mount, {"hindsight-cp-access-key": "cp-key"})

    out = ctx.run(getattr(ctx.on, event)(relation), state)

    data = out.get_relation(relation.id).local_app_data
    assert json.loads(data["port"]) == 9999
    assert json.loads(data["name"]) == "hindsight"


def test_control_plane_ingress_withheld_without_access_key(ctx, pg0_mount):
    state, relation = _cp_ingress_state(pg0_mount, {"tenant-api-key": "hs-key"})

    out = ctx.run(ctx.on.relation_joined(relation), state)

    assert out.get_relation(relation.id).local_app_data == {}
    assert "control plane stopped" in out.unit_status.message


def test_control_plane_ingress_withheld_when_control_plane_disabled(ctx, pg0_mount):
    state, relation = _cp_ingress_state(
        pg0_mount, {"hindsight-cp-access-key": "cp-key"}, cp_enabled=False
    )

    out = ctx.run(ctx.on.relation_joined(relation), state)

    assert out.get_relation(relation.id).local_app_data == {}


def test_control_plane_ingress_withdrawn_when_access_key_removed(ctx, pg0_mount):
    published = {"port": "9999", "name": '"hindsight"', "model": '"m"'}
    state, relation = _cp_ingress_state(
        pg0_mount, {"tenant-api-key": "hs-key"}, app_data=published
    )

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.get_relation(relation.id).local_app_data == {}


def test_api_ingress_is_unaffected_by_control_plane_gating(ctx, pg0_mount):
    secret = testing.Secret(tracked_content={"tenant-api-key": "hs-key"})
    relation = testing.Relation("ingress", remote_app_name="api-ingress")
    state = _state(
        containers={_container(pg0_mount)},
        secrets={secret},
        relations={relation},
        leader=True,
        config={"env-secret": secret.id},
    )

    out = ctx.run(ctx.on.relation_changed(relation), state)

    assert json.loads(out.get_relation(relation.id).local_app_data["port"]) == 8888


def test_relation_removed_without_storage_does_not_suggest_the_action(ctx, pg0_mount):
    container = _container(pg0_mount)
    state = _state(
        containers={container},
        storages=set(),
        leader=True,
        stored_states={_stored(external_database_used=True)},
    )

    out = ctx.run(ctx.on.update_status(), state)

    assert out.unit_status == BlockedStatus("database relation removed: relate postgresql again")


def _cp_running_state(pg0_mount, secret_content, *, related):
    secret = testing.Secret(tracked_content=secret_content)
    relations = (
        {testing.Relation("control-plane-ingress", remote_app_name="cp-ingress")}
        if related
        else set()
    )
    state = _state(
        containers={_running_container(pg0_mount)},
        secrets={secret},
        relations=relations,
        leader=True,
        config={"env-secret": secret.id},
    )
    return state


def test_control_plane_stopped_and_port_closed_with_ingress_but_no_key(ctx, pg0_mount):
    state = _cp_running_state(pg0_mount, {"tenant-api-key": "hs-key"}, related=True)

    out = ctx.run(ctx.on.config_changed(), state)

    container = out.get_container(CONTAINER)
    assert container.service_statuses[CONTROL_PLANE_SERVICE] == pebble.ServiceStatus.INACTIVE
    cp = container.plan.services[CONTROL_PLANE_SERVICE]
    assert cp.startup == "disabled"
    # Even if Pebble restarts it, it demands a key nobody has.
    lockout = cp.environment["HINDSIGHT_CP_ACCESS_KEY"]
    assert len(lockout) >= 32
    assert {p.port for p in out.opened_ports} == {8888}

    again = ctx.run(ctx.on.config_changed(), out)
    env = again.get_container(CONTAINER).plan.services[CONTROL_PLANE_SERVICE].environment
    assert env["HINDSIGHT_CP_ACCESS_KEY"] == lockout


def test_control_plane_runs_with_ingress_and_key(ctx, pg0_mount):
    state = _cp_running_state(pg0_mount, {"hindsight-cp-access-key": "cp-key"}, related=True)

    out = ctx.run(ctx.on.config_changed(), state)

    container = out.get_container(CONTAINER)
    assert container.service_statuses[CONTROL_PLANE_SERVICE] == pebble.ServiceStatus.ACTIVE
    env = container.plan.services[CONTROL_PLANE_SERVICE].environment
    assert env["HINDSIGHT_CP_ACCESS_KEY"] == "cp-key"
    assert {p.port for p in out.opened_ports} == {8888, 9999}


def test_control_plane_without_ingress_still_runs_without_key(ctx, pg0_mount):
    state = _cp_running_state(pg0_mount, {"tenant-api-key": "hs-key"}, related=False)

    out = ctx.run(ctx.on.config_changed(), state)

    container = out.get_container(CONTAINER)
    assert container.service_statuses[CONTROL_PLANE_SERVICE] == pebble.ServiceStatus.ACTIVE
    assert "control plane stopped" not in out.unit_status.message


BANK_ACCESS = """
alice:
  banks: [alice-*]
  read-banks: [shared]
auditor:
  read-banks: ["*"]
"""


def _gateway_state(pg0_mount, *, env=None, keys=None, access=BANK_ACCESS):
    env_secret = testing.Secret(tracked_content=env or {"tenant-api-key": "admin-key"})
    keys_secret = testing.Secret(
        tracked_content=keys or {"alice": "k-alice", "auditor": "k-auditor"}
    )
    return _state(
        containers={_container(pg0_mount)},
        secrets={env_secret, keys_secret},
        leader=True,
        config={
            "env-secret": env_secret.id,
            "access-keys": keys_secret.id,
            "bank-access": access,
        },
    )


def test_bank_access_puts_the_gateway_in_front_of_the_api(ctx, pg0_mount):
    """FR-012, FR-016."""
    out = ctx.run(ctx.on.config_changed(), _gateway_state(pg0_mount))

    container = out.get_container(CONTAINER)
    services = container.plan.services
    api, gateway = services[API_SERVICE].environment, services[GATEWAY_SERVICE]
    assert (api["HINDSIGHT_API_HOST"], api["HINDSIGHT_API_PORT"]) == ("127.0.0.1", "8890")
    assert gateway.startup == "enabled"
    assert "--port 8888" in gateway.command and "app:app_factory" in gateway.command
    env = gateway.environment
    assert env["HINDSIGHT_GATEWAY_UPSTREAM"] == "http://127.0.0.1:8890"
    assert env["HINDSIGHT_GATEWAY_ADMIN_KEY"] == "admin-key"
    assert json.loads(env["HINDSIGHT_GATEWAY_KEYS"]) == {
        "alice": "k-alice",
        "auditor": "k-auditor",
    }
    access = json.loads(env["HINDSIGHT_GATEWAY_ACCESS"])
    assert access["alice"] == {"banks": ["alice-*"], "read-banks": ["shared"]}
    assert container.service_statuses[GATEWAY_SERVICE] == pebble.ServiceStatus.ACTIVE
    # The gateway's code, shipped with the charm, is in the workload.
    fs = container.get_filesystem(ctx)
    for name in ("app.py", "grants.py", "identity.py", "policy.py"):
        assert (fs / GATEWAY_DIR.lstrip("/") / name).read_text().startswith("# Copyright")
    # Every module the gateway imports from its own directory is shipped.
    shipped = {p.name for p in (fs / GATEWAY_DIR.lstrip("/")).iterdir()}
    local = {p.stem for p in (Path(charm_module.__file__).parent / "gateway").glob("*.py")}
    sources = [
        p.read_text() for p in (Path(charm_module.__file__).parent / "gateway").glob("*.py")
    ]
    imported = {
        m
        for m in local
        for source in sources
        if f"from {m} import" in source or f"\nimport {m}\n" in source
    }
    imported |= {"app"}
    assert {f"{m}.py" for m in imported} <= shipped
    # 003 FR-011: the metrics port, for Prometheus to scrape the unit.
    assert {p.port for p in out.opened_ports} == {8888, 9999, 9464}


def test_new_gateway_code_restarts_the_gateway(ctx, pg0_mount, monkeypatch):
    """New gateway code in an upgrade changes its Pebble service, so replan restarts it."""
    first = ctx.run(ctx.on.config_changed(), _gateway_state(pg0_mount))
    before = first.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    monkeypatch.setattr(charm_module, "_gateway_digest", lambda: "new-code")

    second = ctx.run(ctx.on.upgrade_charm(), first)

    after = second.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert after["HINDSIGHT_GATEWAY_CODE"] == "new-code" != before["HINDSIGHT_GATEWAY_CODE"]


def test_without_bank_access_the_api_serves_directly(ctx, pg0_mount):
    """FR-014."""
    state = _gateway_state(pg0_mount, access="")

    out = ctx.run(ctx.on.config_changed(), state)

    services = out.get_container(CONTAINER).plan.services
    api = services[API_SERVICE].environment
    assert (api["HINDSIGHT_API_HOST"], api["HINDSIGHT_API_PORT"]) == ("0.0.0.0", "8888")
    assert services[GATEWAY_SERVICE].startup == "disabled"
    # If Pebble's replan starts it anyway, it holds no port and needs no config.
    assert services[GATEWAY_SERVICE].command == "sleep infinity"
    statuses = out.get_container(CONTAINER).service_statuses
    assert statuses.get(GATEWAY_SERVICE) != pebble.ServiceStatus.ACTIVE


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"access": "alice: [unclosed"}, "bank-access is not valid YAML"),
        ({"env": {"llm-api-key": "x"}}, "bank-access needs tenant-api-key"),
        ({"keys": {"alice": "k-alice"}}, "bank-access: auditor: no key"),
        ({"keys": {"alice": "admin-key", "auditor": "k"}}, "admin key"),
        ({"access": "alice: {writes: [x]}"}, "only 'banks' and 'read-banks'"),
        ({"access": "alice: {banks: [2024-01-01]}"}, "quote client names and bank patterns"),
    ],
)
def test_invalid_bank_access_blocks_and_starts_nothing(ctx, pg0_mount, kwargs, message):
    """FR-013."""
    out = ctx.run(ctx.on.config_changed(), _gateway_state(pg0_mount, **kwargs))

    assert out.unit_status.name == "blocked"
    assert message in out.unit_status.message
    assert API_SERVICE not in out.get_container(CONTAINER).plan.services


ISSUER = "https://id.example.com"


def _member_state(pg0_mount, *, env=None, grants=None, access="", config=None):
    env_secret = testing.Secret(tracked_content=env or {"tenant-api-key": "admin-key"})
    grants_secret = testing.Secret(tracked_content=grants or {"key": "grants-key"})
    keys_secret = testing.Secret(tracked_content={"alice": "k-alice", "auditor": "k-auditor"})
    options = {
        "env-secret": env_secret.id,
        "grants-key": grants_secret.id,
        "access-keys": keys_secret.id,
        "bank-access": access,
        "member-issuer": ISSUER,
        "member-audience": "hindsight",
        **(config or {}),
    }
    return _state(
        containers={_container(pg0_mount)},
        secrets={env_secret, grants_secret, keys_secret},
        leader=True,
        config=options,
    )


def test_member_access_alone_puts_the_gateway_in_front_of_the_api(ctx, pg0_mount):
    """002 FR-012: member access runs the gateway without bank-access."""
    out = ctx.run(ctx.on.config_changed(), _member_state(pg0_mount))

    container = out.get_container(CONTAINER)
    services = container.plan.services
    api, gateway = services[API_SERVICE].environment, services[GATEWAY_SERVICE]
    assert (api["HINDSIGHT_API_HOST"], api["HINDSIGHT_API_PORT"]) == ("127.0.0.1", "8890")
    assert gateway.startup == "enabled"
    env = gateway.environment
    assert env["HINDSIGHT_GATEWAY_MEMBER_ISSUER"] == ISSUER
    assert env["HINDSIGHT_GATEWAY_MEMBER_AUDIENCE"] == "hindsight"
    assert env["HINDSIGHT_GATEWAY_GRANTS_KEY"] == "grants-key"
    assert json.loads(env["HINDSIGHT_GATEWAY_KEYS"]) == {}
    # The embedded database: the gateway asks pg0 for its address.
    assert env["HINDSIGHT_GATEWAY_DATABASE_URL"] == api.get("HINDSIGHT_API_DATABASE_URL", "")
    assert container.service_statuses[GATEWAY_SERVICE] == pebble.ServiceStatus.ACTIVE


def test_member_access_works_alongside_per_key_access(ctx, pg0_mount):
    out = ctx.run(ctx.on.config_changed(), _member_state(pg0_mount, access=BANK_ACCESS))

    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert json.loads(env["HINDSIGHT_GATEWAY_KEYS"])["alice"] == "k-alice"
    assert env["HINDSIGHT_GATEWAY_GRANTS_KEY"] == "grants-key"


def test_the_gateway_gets_the_external_database_and_the_proxy(ctx, pg0_mount, monkeypatch):
    """The gateway keeps grants where the API keeps banks, and reaches the issuer
    through the model's proxy."""
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://proxy.example:3128")
    env = {"tenant-api-key": "admin-key", "database-url": "postgresql://u:p@db:5432/hs"}
    out = ctx.run(ctx.on.config_changed(), _member_state(pg0_mount, env=env))

    gateway = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert gateway["HINDSIGHT_GATEWAY_DATABASE_URL"] == "postgresql://u:p@db:5432/hs"
    assert gateway["HTTPS_PROXY"] == gateway["https_proxy"] == "http://proxy.example:3128"


def test_clearing_member_issuer_turns_member_access_off(ctx, pg0_mount):
    """002 FR-014."""
    state = _member_state(pg0_mount, access=BANK_ACCESS, config={"member-issuer": ""})

    out = ctx.run(ctx.on.config_changed(), state)

    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert "HINDSIGHT_GATEWAY_MEMBER_ISSUER" not in env
    assert "HINDSIGHT_GATEWAY_GRANTS_KEY" not in env


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"config": {"member-issuer": "http://id.example.com"}}, "https URL"),
        ({"config": {"member-audience": ""}}, "needs member-audience"),
        ({"grants": {"other": "x"}}, "needs grants-key"),
        ({"grants": {"key": "admin-key"}}, "grants-key: the grants key is the admin key"),
        (
            {"grants": {"key": "k-alice"}, "access": BANK_ACCESS},
            "grants-key: the grants key is the admin key or a client's key",
        ),
        ({"env": {"llm-api-key": "x"}}, "member-issuer needs tenant-api-key"),
    ],
)
def test_invalid_member_access_blocks_and_starts_nothing(ctx, pg0_mount, kwargs, message):
    """002 FR-013."""
    out = ctx.run(ctx.on.config_changed(), _member_state(pg0_mount, **kwargs))

    assert out.unit_status.name == "blocked"
    assert message in out.unit_status.message
    assert API_SERVICE not in out.get_container(CONTAINER).plan.services


# ---- Usage metrics (003) ------------------------------------------------------


def _scrape_jobs(out, relation):
    data = out.get_relation(relation.id).local_app_data
    return json.loads(data["scrape_jobs"]) if "scrape_jobs" in data else None


def test_the_gateway_is_scraped_on_its_metrics_port(ctx, pg0_mount):
    """003 FR-011, SC-003."""
    relation = testing.Relation("metrics-endpoint", remote_app_name="prometheus")
    state = dataclasses.replace(_gateway_state(pg0_mount), relations={relation})

    out = ctx.run(ctx.on.relation_joined(relation), state)

    jobs = _scrape_jobs(out, relation)
    assert [t for job in jobs for c in job["static_configs"] for t in c["targets"]] == ["*:9464"]
    assert jobs[0]["metrics_path"] == "/metrics"
    assert out.get_relation(relation.id).local_unit_data["prometheus_scrape_unit_address"]


def test_without_the_gateway_no_scrape_job_is_published(ctx, pg0_mount):
    """003 FR-011, SC-003: a job published while the gateway ran is withdrawn."""
    relation = testing.Relation(
        "metrics-endpoint",
        remote_app_name="prometheus",
        local_app_data={"scrape_jobs": json.dumps([{"static_configs": []}])},
        local_unit_data={"prometheus_scrape_unit_address": "10.1.2.3"},
    )
    state = dataclasses.replace(_gateway_state(pg0_mount, access=""), relations={relation})

    out = ctx.run(ctx.on.config_changed(), state)

    assert GATEWAY_SERVICE not in [
        n
        for n, s in out.get_container(CONTAINER).service_statuses.items()
        if s == pebble.ServiceStatus.ACTIVE
    ]
    assert _scrape_jobs(out, relation) is None
    assert out.get_relation(relation.id).local_unit_data == {}
    assert 9464 not in {p.port for p in out.opened_ports}


@pytest.mark.parametrize(("leader", "flag"), [(True, "1"), (False, "0")])
def test_only_the_leader_keeps_the_llm_usage(ctx, pg0_mount, leader, flag):
    """003 FR-005: one unit sweeps and serves the totals."""
    state = dataclasses.replace(_gateway_state(pg0_mount), leader=leader)

    out = ctx.run(ctx.on.config_changed(), state)

    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert env["HINDSIGHT_GATEWAY_LLM_USAGE"] == flag
    # Without member access too, the gateway reaches Hindsight's database.
    assert env["HINDSIGHT_GATEWAY_DATABASE_URL"] == ""
    assert env["HINDSIGHT_GATEWAY_HINDSIGHT_SCHEMA"] == "public"


def test_update_status_moves_the_llm_usage_off_a_former_leader(ctx, pg0_mount):
    """003 FR-005, FR-006."""
    state = ctx.run(ctx.on.config_changed(), _gateway_state(pg0_mount))
    state = dataclasses.replace(state, leader=False)

    out = ctx.run(ctx.on.update_status(), state)

    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert env["HINDSIGHT_GATEWAY_LLM_USAGE"] == "0"


def test_behind_the_gateway_hindsight_records_tokens_without_prompts(ctx, pg0_mount):
    """003 FR-008."""
    out = ctx.run(ctx.on.config_changed(), _gateway_state(pg0_mount))

    api = _api_env(out)
    assert api["HINDSIGHT_API_LLM_TRACE_ENABLED"] == "true"
    assert api["HINDSIGHT_API_LLM_TRACE_MAX_CHARS"] == "256"


def _tracing(**kwargs):
    receivers = [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://tempo:4318"}]
    return testing.Relation(
        "tracing",
        remote_app_name="tempo",
        remote_app_data={"receivers": json.dumps(receivers)},
        **kwargs,
    )


@pytest.mark.parametrize("leader", [True, False])
def test_the_gateway_exports_spans_to_the_tracing_endpoint(ctx, pg0_mount, leader):
    """003 FR-016, FR-017, SC-005."""
    relation = _tracing()
    state = dataclasses.replace(_gateway_state(pg0_mount), relations={relation}, leader=leader)

    out = ctx.run(ctx.on.relation_changed(relation), state)

    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert env["HINDSIGHT_GATEWAY_OTLP_ENDPOINT"] == "http://tempo:4318"
    if leader:
        requested = out.get_relation(relation.id).local_app_data["receivers"]
        assert json.loads(requested) == ["otlp_http"]
    # Hindsight's own trace export, which records prompts, stays off.
    assert not [name for name in _api_env(out) if name.startswith("HINDSIGHT_API_OTEL_")]


def test_without_a_tracing_endpoint_no_spans_are_exported(ctx, pg0_mount):
    """003 FR-016, SC-005: before Tempo answers, and once the relation is gone."""
    waiting = testing.Relation("tracing", remote_app_name="tempo")
    state = dataclasses.replace(_gateway_state(pg0_mount), relations={waiting})
    out = ctx.run(ctx.on.relation_changed(waiting), state)
    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert "HINDSIGHT_GATEWAY_OTLP_ENDPOINT" not in env

    relation = _tracing()
    state = dataclasses.replace(_gateway_state(pg0_mount), relations={relation})
    state = ctx.run(ctx.on.relation_changed(relation), state)
    env = state.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert env["HINDSIGHT_GATEWAY_OTLP_ENDPOINT"] == "http://tempo:4318"
    out = ctx.run(ctx.on.relation_broken(state.get_relation(relation.id)), state)
    env = out.get_container(CONTAINER).plan.services[GATEWAY_SERVICE].environment
    assert "HINDSIGHT_GATEWAY_OTLP_ENDPOINT" not in env
