# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Constants describing the Hindsight workload container layout.

The layout matches the upstream standalone image
(``ghcr.io/vectorize-io/hindsight``): the API virtualenv lives under
``/app/api/.venv``, the Control Plane Next.js standalone build under
``/app/control-plane``, and both run as the unprivileged ``hindsight`` user.
"""

CONTAINER = "hindsight"

# Workload identity (UID 1000 in the upstream image). pg0 refuses to run as root.
WORKLOAD_USER = "hindsight"
WORKLOAD_GROUP = "hindsight"
WORKLOAD_HOME = "/home/hindsight"

# API server
API_SERVICE = "hindsight-api"
API_DIR = "/app/api"
API_BIN = "/app/api/.venv/bin/hindsight-api"
API_PORT = 8888

# Control Plane web UI
CONTROL_PLANE_SERVICE = "hindsight-control-plane"
CONTROL_PLANE_DIR = "/app/control-plane"
CONTROL_PLANE_PORT = 9999

# Pebble checks
API_READY_CHECK = "hindsight-api-ready"
CONTROL_PLANE_CHECK = "hindsight-control-plane-up"

# Embedded PostgreSQL (pg0) data directory; backed by the `data` storage.
PG0_DATA_DIR = f"{WORKLOAD_HOME}/.pg0"

# Relations
DATABASE_RELATION = "database"
DATABASE_NAME = "hindsight"
INGRESS_RELATION = "ingress"
# Publishes the Control Plane (port 9999) on its own hostname, only with an access key.
CONTROL_PLANE_INGRESS_RELATION = "control-plane-ingress"

# Built-in API-key tenant extension, enabled when `tenant-api-key` is supplied.
API_KEY_TENANT_EXTENSION = "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension"

SERVICE_PATH = "/app/api/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The volume holding the embedded database; optional with an external one.
DATA_STORAGE = "data"

# Bank-access gateway: per-key bank access in front of the API. When enabled it
# takes the API's public port and the API moves to a loopback-only port.
GATEWAY_SERVICE = "hindsight-gateway"
GATEWAY_DIR = f"{WORKLOAD_HOME}/.hindsight-gateway"
GATEWAY_FILES = ("app.py", "policy.py")
API_INTERNAL_PORT = 8890
UVICORN_BIN = "/app/api/.venv/bin/uvicorn"
