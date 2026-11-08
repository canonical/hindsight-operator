# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Pebble layer for the Hindsight workload container.

This mirrors the two systemd user services of the Hindsight Workshop SDK
(``hindsight-api`` and ``hindsight-control-plane``) as Pebble services.
"""

from __future__ import annotations

from collections.abc import Mapping

import ops

from defaults import (
    API_BIN,
    API_DIR,
    API_PORT,
    API_READY_CHECK,
    API_SERVICE,
    CONTROL_PLANE_CHECK,
    CONTROL_PLANE_DIR,
    CONTROL_PLANE_PORT,
    CONTROL_PLANE_SERVICE,
    GATEWAY_DIR,
    GATEWAY_SERVICE,
    SERVICE_PATH,
    UVICORN_BIN,
    WORKLOAD_GROUP,
    WORKLOAD_HOME,
    WORKLOAD_USER,
)

# Runtime defaults baked into the upstream image's ENV, restated here because
# Pebble does not inherit the image environment.
_BASE_ENV = {
    "HOME": WORKLOAD_HOME,
    "PATH": SERVICE_PATH,
    "PYTHONUNBUFFERED": "1",
    "TRANSFORMERS_VERBOSITY": "error",
    "HF_HUB_VERBOSITY": "error",
    "TOKENIZERS_PARALLELISM": "false",
}


def build_layer(
    api_env: Mapping[str, str],
    control_plane_env: Mapping[str, str],
    control_plane_enabled: bool,
    gateway_env: Mapping[str, str] | None = None,
) -> ops.pebble.LayerDict:
    """Return the Pebble layer for the API, Control Plane and gateway services.

    The gateway is always defined, so a combined layer cannot keep a stale
    one. Without ``gateway_env`` it is disabled and only sleeps: Pebble's replan
    starts a changed service even when it is disabled, and the real gateway
    would crash without its configuration (failing the replan) or hold the
    API's port. The charm then stops it.
    """
    gateway_command = (
        f"{UVICORN_BIN} --app-dir {GATEWAY_DIR} --factory app:app_factory"
        f" --host 0.0.0.0 --port {API_PORT} --no-access-log"
        if gateway_env
        else "sleep infinity"
    )
    return {
        "summary": "Hindsight layer",
        "description": "Pebble layer managed by the hindsight charm",
        "services": {
            API_SERVICE: {
                "override": "replace",
                "summary": "Hindsight memory API server",
                "command": API_BIN,
                "startup": "enabled",
                "user": WORKLOAD_USER,
                "group": WORKLOAD_GROUP,
                "working-dir": API_DIR,
                "environment": {**_BASE_ENV, **api_env},
                # pg0 needs time to checkpoint and flush WAL on shutdown.
                "kill-delay": "30s",
            },
            CONTROL_PLANE_SERVICE: {
                "override": "replace",
                "summary": "Hindsight Control Plane web UI",
                "command": "node server.js",
                "startup": "enabled" if control_plane_enabled else "disabled",
                "after": [API_SERVICE],
                "user": WORKLOAD_USER,
                "group": WORKLOAD_GROUP,
                "working-dir": CONTROL_PLANE_DIR,
                "environment": {**_BASE_ENV, **control_plane_env},
            },
            GATEWAY_SERVICE: {
                "override": "replace",
                "summary": "Per-key bank access in front of the Hindsight API",
                "command": gateway_command,
                "startup": "enabled" if gateway_env else "disabled",
                "after": [API_SERVICE],
                "user": WORKLOAD_USER,
                "group": WORKLOAD_GROUP,
                "working-dir": GATEWAY_DIR if gateway_env else WORKLOAD_HOME,
                "environment": {**_BASE_ENV, **(gateway_env or {})},
            },
        },
        "checks": {
            # Readiness hits /health, which checks the database: a unit whose
            # database is unreachable is pulled out of the Service rather than
            # restarted (same policy as the upstream Helm chart). No liveness
            # check is declared, so a slow first-boot migration or model load
            # never triggers a restart loop.
            API_READY_CHECK: {
                "override": "replace",
                "level": "ready",
                "period": "10s",
                "timeout": "5s",
                "threshold": 3,
                "http": {"url": f"http://localhost:{API_PORT}/health"},
            },
            CONTROL_PLANE_CHECK: {
                "override": "replace",
                "period": "30s",
                "timeout": "5s",
                "threshold": 3,
                "tcp": {"port": CONTROL_PLANE_PORT},
            },
        },
    }
