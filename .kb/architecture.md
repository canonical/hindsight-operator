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


# Concepts

## Modules

- `src/charm.py`: the `CharmBase` subclass. Event wiring, `_desired_state`, `_reconcile`, pg0 volume preparation, the `use-embedded-database` action and status.
- `src/hindsight_env.py`: config, secret and relation data to environment variables for the API and Control Plane.
- `src/workload.py`: the Pebble layer. Two services, `hindsight-api` and `hindsight-control-plane`, and their checks.
- `src/defaults.py`: container paths, ports, service, check and relation names.
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
- `ann-iterative-scan=false` is for pgvector older than 0.8.0.

## Relations

- `database` (`postgresql_client`, limit 1, optional).
- `ingress` (`ingress`): the API only, with the path prefix stripped. The Control Plane cannot be served under a prefix.
