#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes charm for Hindsight, the agent-memory server from Vectorize.

The charm runs the Hindsight API and Control Plane as Pebble services in one
workload container, following the same shape as the Hindsight Workshop SDK:

* :mod:`hindsight_env` -- config/secret/relation data -> service environment
* :mod:`workload`      -- the Pebble layer (services and health checks)
* :mod:`defaults`      -- container layout and constants

Reconciliation is holistic: every relevant event funnels into
:meth:`HindsightCharm._reconcile`, which derives the desired state from
scratch via :meth:`HindsightCharm._desired_state`. Unit status is computed by
the same function in the ``collect-status`` hook, so status and plan never
disagree. The only state carried between hooks is in ``_stored``: whether the
unit has used the external database, and whether preparing pg0 failed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import ops
import yaml
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

import hindsight_env
import workload
from defaults import (
    API_PORT,
    API_READY_CHECK,
    API_SERVICE,
    CONTAINER,
    CONTROL_PLANE_CHECK,
    CONTROL_PLANE_INGRESS_RELATION,
    CONTROL_PLANE_PORT,
    CONTROL_PLANE_SERVICE,
    DATA_STORAGE,
    DATABASE_NAME,
    DATABASE_RELATION,
    GATEWAY_DIR,
    GATEWAY_FILES,
    GATEWAY_SERVICE,
    INGRESS_RELATION,
    PG0_DATA_DIR,
    WORKLOAD_GROUP,
    WORKLOAD_USER,
)
from gateway import policy as gateway_policy

logger = logging.getLogger(__name__)


_GATEWAY_SOURCE = Path(__file__).parent / "gateway"


def _gateway_digest() -> str:
    """A digest of the gateway's code, which changes when an upgrade brings new code."""
    digest = hashlib.sha256()
    for name in GATEWAY_FILES:
        digest.update((_GATEWAY_SOURCE / name).read_bytes())
    return digest.hexdigest()[:16]


class StatusError(Exception):
    """The charm cannot plan the workload; carries the status to report."""

    def __init__(self, status: ops.StatusBase):
        super().__init__(status.message)
        self.status = status


@dataclass(frozen=True)
class DesiredState:
    """Everything needed to render the Pebble layer."""

    api_env: dict[str, str]
    control_plane_env: dict[str, str]
    control_plane_enabled: bool
    embedded_database: bool
    # Embedded mode with more than one planned unit: this unit keeps serving
    # because its pg0 volume already holds the memories, the others block.
    embedded_scale_out: bool = False
    # Environment for the bank-access gateway, or None when it is off.
    gateway_env: dict[str, str] | None = None


class HindsightCharm(ops.CharmBase):
    """Charm the Hindsight agent-memory server."""

    _stored = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container(CONTAINER)
        self._stored.set_default(
            # The unit has served from the `database` relation; losing the
            # relation must not silently fall back to an empty pg0.
            external_database_used=False,
            # Preparing the pg0 volume failed; kept across hooks so status
            # keeps showing the cause until a later reconcile succeeds.
            volume_error=False,
            # Access key that locks a Control Plane that must not serve; random
            # and kept, so it does not change the plan (and restart it) each hook.
            control_plane_lockout_key="",
        )
        self.database = DatabaseRequires(
            self, relation_name=DATABASE_RELATION, database_name=DATABASE_NAME
        )
        self.ingress = IngressPerAppRequirer(
            self, relation_name=INGRESS_RELATION, port=API_PORT, strip_prefix=True
        )
        # No port here, so the library never publishes on its own: the charm
        # decides in _sync_control_plane_ingress.
        self.control_plane_ingress = IngressPerAppRequirer(
            self, relation_name=CONTROL_PLANE_INGRESS_RELATION, strip_prefix=True
        )

        for event in (
            self.on[CONTAINER].pebble_ready,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.secret_changed,
            self.on.data_storage_attached,
            self.database.on.database_created,
            self.database.on.endpoints_changed,
            self.on[DATABASE_RELATION].relation_broken,
            self.on[CONTROL_PLANE_INGRESS_RELATION].relation_joined,
            self.on[CONTROL_PLANE_INGRESS_RELATION].relation_changed,
            self.on.leader_elected,
        ):
            framework.observe(event, self._reconcile)
        # No-op handlers: these only exist so a check transition runs a hook,
        # and with it collect-status, without waiting for update-status.
        framework.observe(self.on[CONTAINER].pebble_check_failed, self._on_check_event)
        framework.observe(self.on[CONTAINER].pebble_check_recovered, self._on_check_event)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)
        framework.observe(self.on.use_embedded_database_action, self._on_use_embedded_database)

    def _on_check_event(self, event: ops.EventBase) -> None:
        logger.debug("Pebble check event: %s", event)

    def _on_update_status(self, event: ops.EventBase) -> None:
        """Pick up a changed model proxy, which Juju fires no hook for.

        Only then: a full reconcile stops the services on any error, which is
        too much to risk every few minutes on a healthy unit.
        """
        if not self.container.can_connect():
            return
        api = self.container.get_plan().services.get(API_SERVICE)
        if api is None:
            return
        wanted = hindsight_env.proxy_environment(os.environ)
        if hindsight_env.planned_proxy(api.environment) != wanted:
            self._reconcile(event)

    def _on_use_embedded_database(self, event: ops.ActionEvent) -> None:
        if self.model.relations[DATABASE_RELATION]:
            event.fail("the database relation still exists; remove it first")
            return
        if not self._has_data_storage():
            event.fail(
                "this unit has no data storage, which the embedded database needs; "
                "redeploy with --storage data=1G, or relate postgresql again"
            )
            return
        self._stored.external_database_used = False
        self._reconcile(event)
        event.set_results({"result": "switched to the embedded database"})

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def _reconcile(self, _: ops.EventBase) -> None:
        if not self.container.can_connect():
            logger.debug("Pebble not ready yet; deferring reconciliation to pebble-ready")
            return

        try:
            state = self._desired_state()
            if state.embedded_database:
                self._prepare_embedded_database()
        except StatusError as exc:
            logger.info("Not starting Hindsight: %s", exc.status.message)
            self._stop_services()
            self._sync_control_plane_ingress(publish=False)
            return

        if not state.embedded_database:
            self._stored.external_database_used = True

        run_control_plane = self._control_plane_should_run(state)
        control_plane_env = state.control_plane_env
        if not run_control_plane:
            # Pebble's replan restarts any service whose configuration changed,
            # even one that is stopped and disabled. So a Control Plane that must
            # not serve gets a random access key nobody knows, and is stopped
            # both before and after the replan.
            control_plane_env = {
                **control_plane_env,
                hindsight_env.CP_ACCESS_KEY: self._control_plane_lockout_key(),
            }
            self._stop_services(CONTROL_PLANE_SERVICE)
        if state.gateway_env:
            self._push_gateway()
        layer = workload.build_layer(
            state.api_env, control_plane_env, run_control_plane, state.gateway_env
        )
        self.container.add_layer(CONTAINER, layer, combine=True)
        self.container.replan()
        if not run_control_plane:
            self._stop_services(CONTROL_PLANE_SERVICE)
        if not state.gateway_env:
            # Pebble's replan restarts changed services even when disabled.
            self._stop_services(GATEWAY_SERVICE)

        ports = [API_PORT] + ([CONTROL_PLANE_PORT] if run_control_plane else [])
        self.unit.set_ports(*ports)
        self._sync_control_plane_ingress(publish=_control_plane_publishable(state))

    def _control_plane_lockout_key(self) -> str:
        """A random access key that locks the Control Plane; kept stable to avoid restarts."""
        if not self._stored.control_plane_lockout_key:
            self._stored.control_plane_lockout_key = secrets.token_urlsafe(32)
        return str(self._stored.control_plane_lockout_key)

    def _control_plane_should_run(self, state: DesiredState) -> bool:
        """Whether to run the Control Plane.

        With an ingress relation it runs only behind a login: an ingress may
        keep routing to it after its relation data is withdrawn, so nothing must
        be listening without the access key.
        """
        if not state.control_plane_enabled:
            return False
        if self.model.get_relation(CONTROL_PLANE_INGRESS_RELATION) is None:
            return True
        return _control_plane_publishable(state)

    def _sync_control_plane_ingress(self, *, publish: bool) -> None:
        """Publish the Control Plane to its ingress, or withdraw it."""
        relation = self.model.get_relation(CONTROL_PLANE_INGRESS_RELATION)
        if relation is None:
            return
        if publish:
            self.control_plane_ingress.provide_ingress_requirements(port=CONTROL_PLANE_PORT)
            return
        # Withdrawing leaves the ingress with no backend for the hostname.
        relation.data[self.unit].clear()
        if self.unit.is_leader():
            relation.data[self.app].clear()

    def _desired_state(self) -> DesiredState:
        """Derive the workload configuration, or raise :class:`StatusError`."""
        secret_content = self._secret_content()
        database = self._database_credentials()
        bank_access = str(self.config.get("bank-access") or "").strip()

        api_env = hindsight_env.api_environment(
            llm_provider=str(self.config["llm-provider"]),
            llm_model=str(self.config["llm-model"]),
            llm_base_url=str(self.config["llm-base-url"]),
            log_level=str(self.config["log-level"]),
            ann_iterative_scan=bool(self.config["ann-iterative-scan"]),
            worker_id=self.unit.name.replace("/", "-"),
            extra_env=str(self.config["extra-env"]),
            secret_content=secret_content,
            database=database,
            proxy_env=hindsight_env.proxy_environment(os.environ),
            behind_gateway=bool(bank_access),
        )
        gateway_env = self._gateway_environment(bank_access, api_env) if bank_access else None

        embedded = hindsight_env.uses_embedded_database(api_env)
        scale_out = False
        if embedded:
            if self._stored.external_database_used:
                # Only suggest the action where it can work: it needs the volume.
                raise StatusError(
                    ops.BlockedStatus(
                        "database relation removed: relate postgresql again, "
                        "or run use-embedded-database"
                        if self._has_data_storage()
                        else "database relation removed: relate postgresql again"
                    )
                )
            if not self.config["allow-embedded-database"]:
                raise StatusError(
                    ops.BlockedStatus(
                        "no database: relate postgresql (allow-embedded-database is off)"
                    )
                )
            if self.app.planned_units() > 1:
                # Only a unit whose volume already holds memories keeps
                # serving; a new unit would start a second, separate memory.
                if not self._pg0_has_data():
                    raise StatusError(
                        ops.BlockedStatus(
                            "embedded database supports one unit: relate postgresql to scale"
                        )
                    )
                scale_out = True
            if not self._has_data_storage():
                # Without the volume, pg0 would write to the container's own
                # filesystem and lose every memory when the pod restarts.
                raise StatusError(ops.BlockedStatus(_NO_STORAGE_MESSAGE))

        return DesiredState(
            api_env=api_env,
            control_plane_env=hindsight_env.control_plane_environment(api_env, secret_content),
            control_plane_enabled=bool(self.config["control-plane-enabled"]),
            embedded_database=embedded,
            embedded_scale_out=scale_out,
            gateway_env=gateway_env,
        )

    def _gateway_environment(self, bank_access: str, api_env: dict[str, str]) -> dict[str, str]:
        """Validate the per-key bank access settings; raise StatusError if invalid."""
        try:
            access = yaml.safe_load(bank_access) or {}
        except yaml.YAMLError as exc:
            raise StatusError(ops.BlockedStatus("bank-access is not valid YAML")) from exc
        admin_key = api_env.get(hindsight_env.TENANT_API_KEY, "")
        if not admin_key:
            raise StatusError(
                ops.BlockedStatus("bank-access needs tenant-api-key in env-secret (the admin key)")
            )
        keys = self._read_secret("access-keys")
        try:
            access_json = json.dumps(access, sort_keys=True)
        except (TypeError, ValueError) as exc:
            # YAML reads unquoted values such as 2024-01-01 as dates.
            raise StatusError(
                ops.BlockedStatus("bank-access: quote client names and bank patterns")
            ) from exc
        try:
            gateway_policy.load_policy(admin_key, keys, access_json)
        except gateway_policy.PolicyError as exc:
            raise StatusError(ops.BlockedStatus(f"bank-access: {exc}")) from exc
        return hindsight_env.gateway_environment(admin_key, keys, access_json, _gateway_digest())

    def _has_data_storage(self) -> bool:
        """Whether this unit has the `data` volume the embedded database needs."""
        return bool(self.model.storages[DATA_STORAGE])

    def _pg0_has_data(self) -> bool:
        """Whether this unit's pg0 volume holds an initialised database."""
        try:
            entries = self.container.list_files(PG0_DATA_DIR)
        except (ops.pebble.PathError, ops.pebble.APIError):
            return False
        return any(entry.name != "lost+found" for entry in entries)

    def _database_credentials(self) -> hindsight_env.DatabaseCredentials | None:
        """Credentials from the `database` relation; None when not related."""
        relations = self.model.relations[DATABASE_RELATION]
        if not relations:
            return None
        relation = relations[0]
        data = self.database.fetch_relation_data([relation.id]).get(relation.id, {})
        username, password, endpoints = (
            data.get("username"),
            data.get("password"),
            data.get("endpoints"),
        )
        if not (username and password and endpoints):
            raise StatusError(
                ops.WaitingStatus("waiting for credentials over the database relation")
            )
        return hindsight_env.DatabaseCredentials(
            username=username,
            password=password,
            endpoints=endpoints,
            database=data.get("database") or DATABASE_NAME,
        )

    def _secret_content(self) -> dict[str, str]:
        return self._read_secret("env-secret")

    def _read_secret(self, option: str) -> dict[str, str]:
        """Content of the user secret named by a secret-typed config option."""
        secret_id = self.config.get(option)
        if not secret_id:
            return {}
        try:
            return self.model.get_secret(id=str(secret_id)).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as exc:
            logger.warning("Could not read %s %s: %s", option, secret_id, exc)
            raise StatusError(
                ops.BlockedStatus(
                    f"cannot read {option}: run `juju grant-secret <secret> {self.app.name}`"
                )
            ) from exc

    def _prepare_embedded_database(self) -> None:
        """Ensure the pg0 storage mount is writable by the workload user.

        Kubernetes volumes are mounted root-owned, while pg0 (PostgreSQL)
        refuses to run as root, so hand the directory to `hindsight` once.
        """
        try:
            self._make_pg0_writable()
        except (
            ops.pebble.ExecError,
            ops.pebble.ChangeError,
            ops.pebble.APIError,
            ops.pebble.PathError,
        ) as exc:
            logger.error("Preparing %s failed: %s", PG0_DATA_DIR, exc)
            self._stored.volume_error = True
            raise StatusError(_VOLUME_ERROR) from exc
        self._stored.volume_error = False

    def _make_pg0_writable(self) -> None:
        try:
            info = self.container.list_files(PG0_DATA_DIR, itself=True)[0]
        except (ops.pebble.PathError, ops.pebble.APIError):
            # Missing: Pebble reports PathError ("not-found"); make it ourselves.
            self.container.make_dir(
                PG0_DATA_DIR, make_parents=True, user=WORKLOAD_USER, group=WORKLOAD_GROUP
            )
            return
        if info.user == WORKLOAD_USER:
            return
        logger.info("Handing %s to %s", PG0_DATA_DIR, WORKLOAD_USER)
        self.container.exec(
            ["chown", "-R", f"{WORKLOAD_USER}:{WORKLOAD_GROUP}", PG0_DATA_DIR]
        ).wait()

    def _push_gateway(self) -> None:
        """Put the gateway's code, shipped with the charm, into the workload."""
        for name in GATEWAY_FILES:
            self.container.push(
                f"{GATEWAY_DIR}/{name}",
                (_GATEWAY_SOURCE / name).read_text(),
                make_dirs=True,
                permissions=0o644,
                user=WORKLOAD_USER,
                group=WORKLOAD_GROUP,
            )

    def _api_crash_looping(self) -> bool:
        """Whether Pebble is restarting the API, or the gateway in front of it.

        ops has no enum member for Pebble's "backoff", so it passes the raw
        string through.
        """
        services = self.container.get_services(API_SERVICE, GATEWAY_SERVICE)
        for name, info in services.items():
            if name == GATEWAY_SERVICE and info.startup != ops.pebble.ServiceStartup.ENABLED:
                continue
            if getattr(info.current, "value", info.current) in ("backoff", "error"):
                return True
        return False

    def _stop_services(self, *names: str) -> None:
        services = self.container.get_services(*names)
        running = [name for name, svc in services.items() if svc.is_running()]
        if running:
            self.container.stop(*running)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        if not self.container.can_connect():
            event.add_status(ops.WaitingStatus("waiting for Pebble in workload container"))
            return
        try:
            state = self._desired_state()
        except StatusError as exc:
            event.add_status(exc.status)
            return
        if state.embedded_database and self._stored.volume_error:
            event.add_status(_VOLUME_ERROR)
            return

        if API_SERVICE not in self.container.get_plan().services:
            event.add_status(ops.MaintenanceStatus("configuring Hindsight"))
            return

        checks = self.container.get_checks(API_READY_CHECK, CONTROL_PLANE_CHECK)
        # The Pebble check keeps its last result across a service restart, so
        # right after a replan it can still read "up" while the API is down.
        # The charm container shares the pod network, so probe directly too.
        if not (_check_passing(checks.get(API_READY_CHECK)) and api_responding()):
            if self._api_crash_looping():
                event.add_status(
                    ops.WaitingStatus(
                        "Hindsight API keeps exiting: see `pebble logs hindsight-api` "
                        "in the hindsight container"
                    )
                )
            else:
                event.add_status(ops.WaitingStatus("waiting for Hindsight API to become ready"))
            return

        notes = []
        if state.embedded_scale_out:
            notes.append("embedded database; other units blocked")
        elif state.embedded_database:
            notes.append("embedded database")
        run_control_plane = self._control_plane_should_run(state)
        if state.control_plane_enabled and not run_control_plane:
            notes.append(
                "control plane stopped: its ingress needs hindsight-cp-access-key in env-secret"
            )
        if run_control_plane:
            cp = checks.get(CONTROL_PLANE_CHECK)
            if cp is not None and cp.status != ops.pebble.CheckStatus.UP:
                notes.append("control plane down")
        event.add_status(ops.ActiveStatus("; ".join(notes)))


_VOLUME_ERROR = ops.BlockedStatus(f"cannot make {PG0_DATA_DIR} writable; see juju debug-log")


def _control_plane_publishable(state: DesiredState) -> bool:
    """Whether the Control Plane may be exposed: running, and behind a login."""
    return state.control_plane_enabled and bool(
        state.control_plane_env.get(hindsight_env.CP_ACCESS_KEY)
    )


_NO_STORAGE_MESSAGE = (
    "no database: relate postgresql, or redeploy with --storage data=1G for the embedded one"
)


def api_responding(timeout: float = 3.0) -> bool:
    """Whether the API answers /health on the pod-local port right now."""
    try:
        with urllib.request.urlopen(f"http://localhost:{API_PORT}/health", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _check_passing(check: ops.pebble.CheckInfo | None) -> bool:
    """Whether a check is up *and* has actually succeeded.

    Pebble reports a freshly added check as "up" until it has failed
    `threshold` times, so "up" alone would report Active before the API has
    ever answered. `successes` is None on Pebble versions that predate it.
    """
    if check is None or check.status != ops.pebble.CheckStatus.UP:
        return False
    return check.successes is None or check.successes > 0


if __name__ == "__main__":  # pragma: nocover
    # Controller-side storage: the flags in _stored must survive pod churn.
    ops.main(HindsightCharm, use_juju_for_storage=True)
