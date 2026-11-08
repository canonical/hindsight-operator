# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Pure functions that turn charm config, secrets and relation data into
the environment for the Hindsight API and Control Plane services.

Nothing in here touches the Juju model or the workload container, so it is
straightforward to unit test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from defaults import API_INTERNAL_PORT, API_KEY_TENANT_EXTENSION, API_PORT, CONTROL_PLANE_PORT

TENANT_API_KEY = "HINDSIGHT_API_TENANT_API_KEY"
TENANT_EXTENSION = "HINDSIGHT_API_TENANT_EXTENSION"
CP_ACCESS_KEY = "HINDSIGHT_CP_ACCESS_KEY"
DATABASE_URL = "HINDSIGHT_API_DATABASE_URL"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
# The image bundles its embedding and reranker models and LiteLLM's price map.
# Without these, startup calls huggingface.co, and a proxy's refusal is fatal.
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}


@dataclass(frozen=True)
class DatabaseCredentials:
    """Connection details for an external PostgreSQL database."""

    username: str
    password: str
    endpoints: str
    database: str

    def url(self) -> str:
        """Return a ``postgresql://`` URL for the first (primary) endpoint."""
        host = self.endpoints.split(",")[0].strip()
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"postgresql://{user}:{password}@{host}/{self.database}"


def normalise_secret_key(key: str) -> str:
    """Map a Juju secret key to the env var Hindsight expects.

    Juju secret keys are lower-case with dashes. Keys already scoped to
    Hindsight are kept as-is; anything else is treated as an API setting::

        llm-api-key             -> HINDSIGHT_API_LLM_API_KEY
        tenant-api-key          -> HINDSIGHT_API_TENANT_API_KEY
        hindsight-cp-access-key -> HINDSIGHT_CP_ACCESS_KEY
    """
    name = key.strip().upper().replace("-", "_")
    if name.startswith("HINDSIGHT_"):
        return name
    return f"HINDSIGHT_API_{name}"


def parse_extra_env(raw: str) -> dict[str, str]:
    """Parse newline-separated ``KEY=VALUE`` pairs, ignoring blanks and comments."""
    env: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            env[key.strip()] = value.strip()
    return env


def planned_proxy(env: Mapping[str, str]) -> dict[str, str]:
    """The proxy variables, in either spelling, of a service environment."""
    return {k: v for k, v in env.items() if k.upper() in PROXY_VARS}


def proxy_environment(charm_env: Mapping[str, str]) -> dict[str, str]:
    """Translate the model's Juju proxy settings for the workload.

    Juju only gives these to the charm. Both spellings are set because HTTP
    clients disagree on which one they read.
    """
    env: dict[str, str] = {}
    for name in PROXY_VARS:
        if value := charm_env.get(f"JUJU_CHARM_{name}"):
            env[name] = value
            env[name.lower()] = value
    return env


def api_environment(
    *,
    llm_provider: str,
    llm_model: str,
    llm_base_url: str,
    log_level: str,
    worker_id: str,
    ann_iterative_scan: bool = True,
    extra_env: str,
    secret_content: Mapping[str, str],
    database: DatabaseCredentials | None,
    proxy_env: Mapping[str, str] | None = None,
    behind_gateway: bool = False,
) -> dict[str, str]:
    """Build the environment for the ``hindsight-api`` service.

    Precedence (lowest to highest): charm defaults, typed options and the
    model's proxy settings, ``extra-env``, ``env-secret`` content, then
    charm-owned settings that must not be overridden (bind address/port and the
    database URL). A proxy variable in ``extra-env`` replaces both spellings
    from the model, so a client preferring the other one can't pick up a stale
    value.
    """
    env: dict[str, str] = {
        **OFFLINE_ENV,
        "HINDSIGHT_API_LLM_PROVIDER": llm_provider,
        "HINDSIGHT_API_LLM_MODEL": llm_model,
        "HINDSIGHT_API_LOG_LEVEL": log_level,
        "HINDSIGHT_API_WORKER_ID": worker_id,
        "HINDSIGHT_API_ANN_ITERATIVE_SCAN": "true" if ann_iterative_scan else "false",
    }
    if llm_base_url:
        env["HINDSIGHT_API_LLM_BASE_URL"] = llm_base_url

    extra = parse_extra_env(extra_env)
    operator_set = {key.upper() for key in extra}
    env.update({k: v for k, v in (proxy_env or {}).items() if k.upper() not in operator_set})
    env.update(extra)
    env.update({normalise_secret_key(k): v for k, v in (secret_content or {}).items()})

    # API-key auth: supplying a tenant key switches on the built-in extension
    # unless the operator chose a different extension explicitly.
    if env.get(TENANT_API_KEY):
        env.setdefault(TENANT_EXTENSION, API_KEY_TENANT_EXTENSION)

    # Control Plane settings are not meant for the API process.
    env = {k: v for k, v in env.items() if not k.startswith("HINDSIGHT_CP_")}

    if behind_gateway:
        # Only the bank-access gateway, in the same pod, may reach the API.
        env["HINDSIGHT_API_HOST"] = "127.0.0.1"
        env["HINDSIGHT_API_PORT"] = str(API_INTERNAL_PORT)
    else:
        env["HINDSIGHT_API_HOST"] = "0.0.0.0"
        env["HINDSIGHT_API_PORT"] = str(API_PORT)
    if database is not None:
        # The relation always wins over a manually supplied URL.
        env[DATABASE_URL] = database.url()
    elif not env.get(DATABASE_URL):
        # No URL at all means Hindsight's embedded pg0 (the upstream default).
        env.pop(DATABASE_URL, None)
    return env


def uses_embedded_database(api_env: Mapping[str, str]) -> bool:
    """Whether the API will fall back to its embedded pg0 database."""
    url = api_env.get(DATABASE_URL, "")
    return not url or url.startswith("pg0")


def control_plane_environment(api_env: Mapping[str, str], secret_content: Mapping[str, str]):
    """Build the environment for the ``hindsight-control-plane`` service."""
    secrets = {normalise_secret_key(k): v for k, v in (secret_content or {}).items()}
    env = {
        "NODE_ENV": "production",
        "HOSTNAME": "0.0.0.0",
        "PORT": str(CONTROL_PLANE_PORT),
        "HINDSIGHT_CP_DATAPLANE_API_URL": f"http://localhost:{API_PORT}",
    }
    env.update({k: v for k, v in secrets.items() if k.startswith("HINDSIGHT_CP_")})
    if api_env.get(TENANT_API_KEY):
        env["HINDSIGHT_CP_DATAPLANE_API_KEY"] = api_env[TENANT_API_KEY]
    return env


def gateway_environment(
    admin_key: str, keys: Mapping[str, str], access_json: str, code_digest: str
) -> dict[str, str]:
    """Environment for the bank-access gateway service.

    ``code_digest`` identifies the gateway's code, so that Pebble restarts the
    gateway when a charm upgrade brings new code.
    """
    return {
        "HINDSIGHT_GATEWAY_UPSTREAM": f"http://127.0.0.1:{API_INTERNAL_PORT}",
        "HINDSIGHT_GATEWAY_ADMIN_KEY": admin_key,
        "HINDSIGHT_GATEWAY_KEYS": json.dumps(dict(keys), sort_keys=True),
        "HINDSIGHT_GATEWAY_ACCESS": access_json,
        "HINDSIGHT_GATEWAY_CODE": code_digest,
    }
