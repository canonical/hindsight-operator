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

## §8 - Members are judged as clients are

A member's request is classified by the same `classify` as a client key's, and a member's access is a read-write client's on exactly the banks granted to them, less creating and deleting a bank. Do not add a second classification for members.

## §9 - Grants in their own schema

The gateway keeps grants in the `hindsight_gateway` schema of Hindsight's database and never writes Hindsight's own tables. Bank creation goes through Hindsight's API with the admin key.

## §10 - Usage read from `llm_requests` only

The only Hindsight table the gateway reads is `llm_requests`, for the LLM usage totals it keeps in the `hindsight_gateway` schema. It attributes an LLM call to a caller by joining the record's `metadata.document_id` to the documents it saw retained, never by reading memories or documents. No email, key or token goes into a metric label or a span.


# Concepts

## Modules

- `src/charm.py`: the `CharmBase` subclass. Event wiring, `_desired_state`, `_reconcile`, pg0 volume preparation, the `use-embedded-database` action and status.
- `src/hindsight_env.py`: config, secret and relation data to environment variables for the API and Control Plane.
- `src/workload.py`: the Pebble layer. Two services, `hindsight-api` and `hindsight-control-plane`, and their checks.
- `src/defaults.py`: container paths, ports, service, check and relation names.
- `src/gateway/`: the per-key bank-access gateway, shipped in the charm and pushed into the workload container. `policy.py` is pure: configuration parsing, key identification and request classification. `app.py` is the Starlette proxy. `identity.py` verifies member tokens, and `grants.py` keeps grants in PostgreSQL. `usage.py` counts requests and serves the metrics port, `llm_usage.py` keeps the LLM usage totals and prices them, `attribution.py` records the documents each retain names, and `spans.py` continues the client's trace and exports the gateway's spans.
- `lib/charms/`: vendored charm libraries, `data_platform_libs.v0.data_interfaces`, `traefik_k8s.v2.ingress`, `prometheus_k8s.v0.prometheus_scrape` and `tempo_coordinator_k8s.v0.tracing`. Update them with `charmcraft fetch-libs`, never by hand.

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
- `member-issuer` and `member-audience` turn on member access, and `grants-key` names a Juju secret whose `key` the grants API accepts. Member access needs `tenant-api-key` as the admin key. The gateway runs while either `bank-access` or `member-issuer` is set.
- `ann-iterative-scan=false` is for pgvector older than 0.8.0.

## Relations

- `database` (`postgresql_client`, limit 1, optional).
- `ingress` (`ingress`): the API only, with the path prefix stripped. The Control Plane cannot be served under a prefix.
- `metrics-endpoint` (`prometheus_scrape`, provided): a scrape job for port 9464 on every unit while `bank-access` or `member-issuer` is set. Otherwise the charm creates no `MetricsEndpointProvider`, which would publish a default job on port 80, and clears the relation data.
- `tracing` (`tracing`, limit 1, optional): the leader requests `otlp_http`, and every unit passes the provider's endpoint to the gateway as `HINDSIGHT_GATEWAY_OTLP_ENDPOINT`. The API's own trace settings are left alone.

## Per-key bank access

- Off (`bank-access` empty): the API listens on `0.0.0.0:8888`. The `hindsight-gateway` Pebble service is defined but disabled, its command is `sleep infinity`, and the charm stops it after each replan. Pebble's replan starts a service whose definition changed even when it is disabled, so a disabled gateway with its real command would crash without its configuration and fail the replan, or hold port 8888.
- On: the gateway listens on `0.0.0.0:8888` and the API on `127.0.0.1:8890`, so nothing outside the pod reaches the API directly. The gateway runs with uvicorn, Starlette and httpx from the image's `/app/api/.venv`, from code the charm pushes to `/home/hindsight/.hindsight-gateway`. The Control Plane keeps calling `localhost:8888` with the admin key.
- The gateway authenticates `Authorization: Bearer`, comparing in constant time with the admin key and every client key. The admin key is forwarded unchanged. A client's allowed request is forwarded with the admin key, its own `Authorization`, `X-Api-Key` and `Cookie` removed. Bodies stream in both directions.
- Requests are classified on the raw, percent-encoded path, which is forwarded unchanged, so the gateway and the API agree on the bank. Paths with `//`, `/./`, `/../`, or a bank segment decoding to `.`, `..`, or containing `/`, `\` or NUL are refused.
- Hindsight 0.9.2's own per-bank hooks (`validate_bank_read`, `validate_bank_write`) are not called on document export, mental-model history, or imports into an existing bank. The gateway does not rely on them.
- In Hindsight 0.9.2 a bank's MCP endpoint takes the bank only from its URL, and its tools take no bank argument. The gateway reads an MCP `POST` body to judge each `tools/call`.

## Member bank access

- The gateway first compares the bearer value with the admin key, the grants key and every client key. Only a value that matches none, and has the three dot-separated parts of a JSON Web Token, is verified as a member token.
- Tokens are verified with PyJWT from the image's venv, against the keys at the `jwks_uri` of `<member-issuer>/.well-known/openid-configuration`, accepting only asymmetric algorithms. The keys are cached for ten minutes, and a token with a key ID the cache does not hold refetches them, at most once a minute.
- The issuer is reached through the model's proxy, which the charm passes to the gateway as to the API.
- Grants are one table, `hindsight_gateway.bank_grants (bank_id, email, granted_at)`, which the gateway creates if it is missing. The gateway reads a member's grants on every request, so a change applies at once on every unit.
- With an external database the gateway connects with the API's `HINDSIGHT_API_DATABASE_URL`. With pg0 it waits for the API to start pg0 and connects to the address pg0 reports.
- `PUT /admin/banks/<bank>` checks `GET /v1/default/banks/<bank>/profile` and, on 404, calls `PUT /v1/default/banks/<bank>` with the admin key.

## Usage metrics

- The gateway serves `GET /metrics` on `0.0.0.0:9464` with a small asyncio listener in the same process, so it reads the same counters as the proxy on 8888. The ingress publishes only 8888.
- Request counts are in memory and start at zero when the gateway restarts, as Prometheus counters may.
- On the leader, `HINDSIGHT_GATEWAY_LLM_USAGE=1` makes the gateway sweep `llm_requests` every 30 seconds. One transaction, under a PostgreSQL advisory lock, adds the records not yet in `hindsight_gateway.llm_usage_counted` to `hindsight_gateway.llm_usage`, and marks them counted. IDs whose record Hindsight has deleted are then removed from `llm_usage_counted`. Leadership moving makes the charm restart both gateways with the setting moved.
- The cost is computed when `/metrics` is read: uncached input tokens at `input_cost_per_token`, cached input tokens at `cache_read_input_token_cost` (or the input price when the list has none) and output tokens at `output_cost_per_token`. The model is looked up as `<provider>/<model>`, then as `<model>`.
- Hindsight's own metrics come from `GET /metrics` on the API's loopback port with the admin key.

## Traces

- The gateway uses the OpenTelemetry SDK from the image's venv, with a `TracerProvider` of its own (service name `hindsight-gateway`), never the global one. Its sampler is the SDK's default, parent based and always on, so a client's sampled flag decides.
- A span is started after a request is classified, so public routes get none, and ended when the response starts. Its attributes are `hindsight.client`, `hindsight.bank` (when allowed), `hindsight.operation`, `http.request.method` and `http.response.status_code`. A 5xx sets the span's status to error.
- The client's `traceparent` and `tracestate` are replaced on the way to Hindsight by the span's. `baggage` passes unchanged. Calls the gateway makes on its own account for a request, such as the bank check of the grants API, carry the same `traceparent`.
- With `HINDSIGHT_GATEWAY_OTLP_ENDPOINT`, a batch processor exports to `<endpoint>/v1/traces`, through the model's proxy, which the charm passes to the gateway. The provider is shut down, and its spans flushed, when the gateway stops.
- Hindsight 0.9.2 writes a `trace_id` of its own making to each `llm_requests` record. When Hindsight continues an incoming trace, that ID joins an LLM call to the gateway's span, and so to its caller (003 Out of Scope).
- Until then, retain calls are joined by document (003 FR-018 to FR-020). Every unit writes `hindsight_gateway.retained_documents` (bank, document, caller, trace, span, sampled) before forwarding a retain, so the row exists before Hindsight's first extraction call. The leader's sweep is one statement: it left joins the new `llm_requests` records to `retained_documents`, adds them to `llm_usage` by bank, client, operation, provider and model, and returns the attributed calls of sampled traces. `spans.llm_calls` then starts each call's span under a remote parent built from the stored trace and span IDs, with the record's own start and end times.
- Hindsight cuts a record's metadata to `HINDSIGHT_API_LLM_TRACE_MAX_CHARS` like its prompt and output, which is why the charm sets 256 and not 1.
