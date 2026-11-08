# Preface

What the charm deploys, how one reconcile turns Juju state into the Hindsight workload, and which module implements each concern. Read this before changing anything under `src/` or `charmcraft.yaml`.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Holistic reconcile

Every state-changing event runs the one `_reconcile` handler in `src/charm.py`, which derives the whole desired state and applies it. Do not add an event handler that changes the workload partially.

## §2 - Pure environment

`src/hindsight_env.py` turns config, secret content, relation data and proxy settings into service environments without touching the Juju model or the container. Keep it pure so it is unit tested without `ops`.

## §3 - Status from collect-status

Unit status is set only in `_on_collect_unit_status`. A reconcile that cannot proceed raises `StatusError`, stops the services and leaves the message for collect-status to report.

## §4 - No silent fallback to pg0

A unit that has served from the `database` relation never switches to the embedded database on its own. It blocks until the relation returns or the `use-embedded-database` action is run.

## §5 - Upstream image

The workload is the upstream `ghcr.io/vectorize-io/hindsight` image, pinned by `upstream-source` in `charmcraft.yaml`. `src/defaults.py` mirrors its container layout. Update both together when the image changes its paths, user or ports.


## §6 - Per-key access is deny by default

While `bank-access` is set, every request on the API's port passes the gateway in `src/gateway/`. A client key reaches only routes the gateway classifies as an operation on one bank the key may use. Add a route to the classification in `src/gateway/policy.py` only after checking what it does in the pinned Hindsight version. Never forward an unclassified route to a client key.

## §7 - One policy

The charm validates `bank-access` with `src/gateway/policy.py`, the same module the gateway enforces. Do not parse or check the configuration a second way.


# Concepts

## Modules

- `src/charm.py`: the `CharmBase` subclass. Event wiring, `_desired_state`, `_reconcile`, pg0 volume preparation, the `use-embedded-database` action and status.
- `src/hindsight_env.py`: config, secret and relation data to environment variables for the API and Control Plane.
- `src/workload.py`: the Pebble layer. Two services, `hindsight-api` and `hindsight-control-plane`, and their checks.
- `src/defaults.py`: container paths, ports, service, check and relation names.
- `src/gateway/`: the per-key bank-access gateway, shipped in the charm and pushed into the workload container. `policy.py` is pure: configuration parsing, key identification and request classification. `app.py` is the Starlette proxy.
- `lib/charms/`: vendored charm libraries, `data_platform_libs.v0.data_interfaces` and `traefik_k8s.v2.ingress`. Update them with `charmcraft fetch-libs`, never by hand.

## Workload

- The API listens on 8888 and the Control Plane on 9999. Both run as the `hindsight` user (UID 1000).
- The API's readiness check is an HTTP probe of `/health`. The Control Plane check is a TCP probe.
- The image bundles its embedding and reranker models and LiteLLM's price map. The charm sets `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` and `LITELLM_LOCAL_MODEL_COST_MAP` so startup makes no network calls.
- The model's `juju-http-proxy`, `juju-https-proxy` and `juju-no-proxy` reach the charm as `JUJU_CHARM_HTTP_PROXY` and so on, and the API as `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in both upper and lower case. Juju fires no hook when they change, so `update-status` compares the planned proxy with the current one and reconciles on a difference.

## Database modes

- Embedded: pg0 inside the API process, stored on the `data` storage volume at `/home/hindsight/.pg0`. Kubernetes mounts the volume root-owned, so the reconcile hands it to `hindsight` once. Only one unit may serve from pg0. With more planned units, a unit whose volume holds no data blocks.
- External: the `database` relation to `postgresql-k8s` with pgvector, or a `database-url` key in `env-secret` for a PostgreSQL that Juju does not manage. Units share memory through it.
- `allow-embedded-database=false` makes a unit without an external database block instead of starting pg0.

## Configuration and secrets

- `env-secret` names a Juju secret. Each key becomes an environment variable: `llm-api-key` becomes `HINDSIGHT_API_LLM_API_KEY`. `tenant-api-key` turns on the API-key tenant extension, and `hindsight-cp-access-key` protects the Control Plane.
- `extra-env` passes other non-secret Hindsight settings.
- `bank-access` maps client names to `banks` and `read-banks` patterns, and `access-keys` names a Juju secret mapping client names to their keys. Per-key access needs `tenant-api-key` as the admin key.
- `ann-iterative-scan=false` is for pgvector older than 0.8.0.

## Relations

- `database` (`postgresql_client`, limit 1, optional).
- `ingress` (`ingress`): the API only, with the path prefix stripped. The Control Plane cannot be served under a prefix.

## Per-key bank access

- Off (`bank-access` empty): the API listens on `0.0.0.0:8888`. The `hindsight-gateway` Pebble service is defined but disabled, its command is `sleep infinity`, and the charm stops it after each replan. Pebble's replan starts a service whose definition changed even when it is disabled, so a disabled gateway with its real command would crash without its configuration and fail the replan, or hold port 8888.
- On: the gateway listens on `0.0.0.0:8888` and the API on `127.0.0.1:8890`, so nothing outside the pod reaches the API directly. The gateway runs with uvicorn, Starlette and httpx from the image's `/app/api/.venv`, from code the charm pushes to `/home/hindsight/.hindsight-gateway`. The Control Plane keeps calling `localhost:8888` with the admin key.
- The gateway authenticates `Authorization: Bearer`, comparing in constant time with the admin key and every client key. The admin key is forwarded unchanged. A client's allowed request is forwarded with the admin key, its own `Authorization`, `X-Api-Key` and `Cookie` removed. Bodies stream in both directions.
- Requests are classified on the raw, percent-encoded path, which is forwarded unchanged, so the gateway and the API agree on the bank. Paths with `//`, `/./`, `/../`, or a bank segment decoding to `.`, `..`, or containing `/`, `\` or NUL are refused.
- Hindsight 0.9.2's own per-bank hooks (`validate_bank_read`, `validate_bank_write`) are not called on document export, mental-model history, or imports into an existing bank. The gateway does not rely on them.
- In Hindsight 0.9.2 a bank's MCP endpoint takes the bank only from its URL, and its tools take no bank argument. The gateway reads an MCP `POST` body to judge each `tools/call`.

