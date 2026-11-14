# Hindsight Operator

## Description

The Hindsight Operator deploys and operates [Hindsight](https://github.com/vectorize-io/hindsight), Vectorize's agent-memory server, on Kubernetes.

This operator provides:
- The Hindsight API (port 8888) and Control Plane web UI (port 9999)
- An embedded PostgreSQL for single-unit deployments
- An external PostgreSQL with pgvector, for scaling out with shared memory
- LLM provider configuration, with keys held in a Juju secret
- Optional API-key authentication
- Ingress for the API

It is the Kubernetes counterpart of the [Hindsight Workshop SDK](https://github.com/mz2/hindsight-sdk) and uses the upstream `ghcr.io/vectorize-io/hindsight` image.

## Usage

Bootstrap a Kubernetes controller to Juju (Juju 3.3+) and create a model:

```shell
juju add-model hindsight
```

To deploy a single unit with the embedded database:

```shell
juju deploy hindsight --channel latest/edge --storage data=1G
```

The embedded database lives on the `data` storage volume and supports one unit. Without `--storage data=1G` the charm blocks rather than keep memories on the container's own filesystem, which a pod restart would wipe. Data is not migrated between the embedded and an external database.

### LLM provider

By default `llm-provider=none`: retain and recall work, but reflection does not. To use a provider, put its key in a Juju secret:

```shell
juju add-secret hindsight-keys llm-api-key=sk-...
juju grant-secret hindsight-keys hindsight
juju config hindsight llm-provider=openai llm-model=gpt-4o-mini \
  env-secret=secret:<id>
```

If the model has a proxy (`juju-https-proxy`), the charm passes it to Hindsight, and the proxy must allow your provider's host. Nothing else needs internet access: the charm runs Hindsight's bundled models offline. To use a different local model, set `HF_HUB_OFFLINE=0` in `extra-env`, and allow `huggingface.co` if proxied. Secret keys become Hindsight environment variables: `llm-api-key` becomes `HINDSIGHT_API_LLM_API_KEY`, and so on. Setting `tenant-api-key` turns on API-key authentication, and `hindsight-cp-access-key` protects the Control Plane. Use `extra-env` for any other non-secret setting.

## Relations / Integrations

### Database

To scale beyond one unit, relate to PostgreSQL. Hindsight keeps everything there, so it needs no `data` storage: deploy it without `--storage`. Hindsight needs pgvector, which must be enabled on the PostgreSQL charm:

```shell
juju deploy postgresql-k8s --channel 14/stable --trust \
  --config plugin_vector_enable=true    # 16 track: plugin-vector-enable=true
juju deploy hindsight --channel latest/edge --config allow-embedded-database=false
juju integrate hindsight postgresql-k8s
juju add-unit hindsight -n 2
```

With pgvector older than 0.8.0, also set `ann-iterative-scan=false`. Hindsight otherwise logs a warning and drops the setting, and filtered searches can return fewer results either way.

For a PostgreSQL that Juju doesn't manage, set `database-url=postgresql://...` in `env-secret` instead.

If the relation is later removed, the charm blocks rather than switching to an empty embedded database. Relate again, or run `juju run hindsight/0 use-embedded-database` to start over on the embedded database.

### Ingress

To expose the API, for example through Traefik:

```shell
juju integrate hindsight:ingress traefik-k8s
```

The Control Plane web UI (port 9999) cannot be served under a path prefix, so it gets its own hostname through the separate `control-plane-ingress` endpoint, for example with `ingress-configurator` in front of an HAProxy ingress:

```shell
juju deploy ingress-configurator hindsight-ui-ingress --trust --config hostname=<ui-hostname>
juju integrate hindsight-ui-ingress:haproxy-route <haproxy-app-or-offer>
juju integrate hindsight:control-plane-ingress hindsight-ui-ingress:ingress
```

While this relation exists, the Control Plane runs only with `hindsight-cp-access-key` set in `env-secret`. Without it the charm stops the UI and withdraws the route, so it is never reachable without a login.

Without an ingress, reach the UI with `kubectl port-forward svc/hindsight 9999 -n <model>`.

### Observability

To have the Canonical Observability Stack (COS) scrape the charm's metrics:

```shell
juju integrate hindsight:metrics-endpoint prometheus-k8s
```

To have COS keep the gateway's traces:

```shell
juju integrate hindsight:tracing tempo
```

The metrics and traces come from the gateway, so they exist while `bank-access` or `member-issuer` is set. See [Usage metrics](#usage-metrics) and [Traces](#traces).

## Per-key bank access

Give each client application its own API key, limited to the memory banks it may use. `banks` are read and write, `read-banks` are read only, and names may be shell-style patterns:

```shell
juju add-secret hindsight-access-keys \
  medic-agent=$(openssl rand -hex 32) auditor=$(openssl rand -hex 32)
juju grant-secret hindsight-access-keys hindsight
juju config hindsight access-keys=secret:<id> bank-access='
medic-agent:
  banks: [medic-*]
  read-banks: [shared]
auditor:
  read-banks: ["*"]
'
```

Clients send their key as `Authorization: Bearer <key>`. A key then:

- lists, recalls from and reflects on only the banks it may read
- stores in, changes, deletes and creates only banks matching its `banks`
- gets on `/mcp/<bank>/` only the MCP tools its access to that bank allows
- is refused anything that spans banks: `/metrics`, chunks and files fetched by ID, and the multi-bank `/mcp/` endpoint

Anything the charm does not recognise as an operation on one bank is refused to client keys, and each refusal is logged with the client's name, never its key. The admin key (`tenant-api-key` in `env-secret`, required) keeps full access, and the Control Plane uses it. All banks stay in one database schema: a gateway in the Hindsight pod enforces the access, not PostgreSQL.

An invalid `bank-access`, a client without a key, or a missing admin key blocks the unit with a message. Clear `bank-access` to turn the feature off: the API then accepts only the admin key.

## Member bank access

Let people reach the banks granted to their email with a token from your identity provider, and let a provisioning system grant and withdraw those banks. Put the key the provisioning system uses in a secret, and name your OpenID Connect issuer and the audience its tokens are issued for:

```shell
juju add-secret hindsight-grants-key key=$(openssl rand -hex 32)
juju grant-secret hindsight-grants-key hindsight
juju config hindsight grants-key=secret:<id> \
  member-issuer=https://identity.example.com \
  member-audience=hindsight
```

Member access needs the admin key (`tenant-api-key` in `env-secret`), as per-key access does, and works alongside it.

A member sends their token as `Authorization: Bearer <token>`. The gateway checks its signature against the issuer's published keys, its issuer, audience and expiry, and reads the person's `email` claim. A member then:

- lists, recalls from, reflects on, stores in and changes only the banks granted to their email
- gets on `/mcp/<bank>/` the bank's read and write MCP tools, on a granted bank only
- cannot create or delete a bank, or reach anything that spans banks

The provisioning system sends the grants key as `Authorization: Bearer <key>` to the grants API. The key reaches nothing else, so it cannot read memories:

| Request | Effect |
|---|---|
| `PUT /admin/banks/<bank>` | creates the bank if it does not exist |
| `GET /admin/banks/<bank>/grants` | lists the emails granted the bank, as `{"grants": [...]}`, or 404 if there is no such bank |
| `PUT /admin/banks/<bank>/grants/<email>` | grants the bank to the email, or 404 if there is no such bank |
| `DELETE /admin/banks/<bank>/grants/<email>` | withdraws the grant, or 404 if there is none |

A grant applies to the member's next request on every unit. Grants are kept in Hindsight's database, in a `hindsight_gateway` schema of their own, so they are shared by every unit and kept when member access is turned off. Clear `member-issuer` to turn member access off.

## Usage metrics

While the gateway runs, each unit serves metrics in the Prometheus text format at `http://<unit>:9464/metrics`. The port is not published through `ingress`.

- `hindsight_gateway_requests_total` counts the requests each unit answered, labeled by `client`, `bank`, `operation` and `code`. `client` is the client's name from `bank-access`, or `admin`, `grants`, `member` or `unauthenticated`. Members are counted together: no email reaches the metrics. `bank` is set only on an allowed request. `operation` is one of `retain`, `recall`, `reflect`, `read`, `write`, `delete-bank`, `list-banks`, `mcp`, `grants`, `admin` and `invalid`. `code` is the status class, such as `2xx`.
- `hindsight_gateway_llm_calls_total`, `hindsight_gateway_llm_tokens_total` (with `type` `input`, `output` or `cached`) and `hindsight_gateway_llm_cost_usd_total` are the LLM calls, tokens and cost of each bank, labeled by `bank`, `client`, `operation`, `provider` and `model`. `client` is the caller that retained the document an LLM call extracts from, named as in `hindsight_gateway_requests_total`, or `unattributed` (see below). `operation` is Hindsight's, such as `retain`, `reflect` or `consolidation`. Sum over `client` and `operation` for a bank's total. Only the leader unit serves them, so a sum across units counts each call once. The cost is priced with the LiteLLM price list in the Hindsight image, and is missing for a model the list does not name.
- `hindsight_gateway_llm_usage_up` is 0 while Hindsight's database cannot be read.
- Hindsight's own metrics follow.

The LLM totals come from the record Hindsight keeps of each LLM call, which it deletes after `HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS` (one day by default). The leader adds each record to the totals once, a minute or less after Hindsight writes it, and the totals are kept in the `hindsight_gateway` schema. While the gateway runs, the charm has Hindsight keep the record with at most 256 characters of each prompt, output and metadata, by setting `HINDSIGHT_API_LLM_TRACE_ENABLED=true` and `HINDSIGHT_API_LLM_TRACE_MAX_CHARS=256`. The start of a prompt is Hindsight's own instructions, and the start of an output can hold facts extracted from a retain, which Hindsight also keeps as memories. Setting either in `extra-env` or `env-secret` overrides the charm, and turning tracing off stops the LLM totals.

Hindsight 0.9.2 does not record which request caused an LLM call, but it records the document a retain's extraction call reads. Before forwarding a retain, over the HTTP API or an MCP `retain` or `sync_retain` tool, the gateway records each document ID the retain names, with the caller and the request's span, for 7 days. The leader totals each extraction call under the caller that last retained its document in that bank. These calls stay `unattributed`:

- the calls of a retain item sent without a `document_id`, which Hindsight gives an ID the gateway never sees. Give each item a `document_id` to have its calls attributed.
- the calls of a document ID longer than about 230 characters, which does not fit in the metadata Hindsight keeps.
- reflect, consolidation and mental model calls, and file retains.

The gateway never changes a retain. If the record cannot be written within 2 seconds, the retain is forwarded and its calls stay `unattributed`.

## Traces

While the gateway runs, it records a span for each request it counts in the metrics, with the same `client`, `bank`, `operation` and status as attributes. A request that arrives with a W3C `traceparent` header gets its span in that trace; any other request starts a new trace. The call to Hindsight carries a `traceparent` naming the gateway's span as its parent. No key, token, email, path or body goes into a span.

Related to a tracing provider such as Tempo over `tracing`, the gateway exports its spans over OTLP HTTP. Without the relation, trace headers are still passed on, and nothing is exported.

Hindsight 0.9.2 does not continue the trace it receives: it starts a trace of its own for each operation. The leader adds a span for each LLM call it attributes to a retain, as above, under that retain's gateway span, once Hindsight has recorded the call. The span carries the call's start and end times, provider, model and tokens, and no prompt, output or document ID. Other LLM calls, such as reflect's, do not appear in the client's trace. The charm does not turn on Hindsight's own trace export (`HINDSIGHT_API_OTEL_TRACES_ENABLED`), which records whole prompts and outputs.

## Security

Without `tenant-api-key`, the API accepts unauthenticated requests from anything that can reach it. Set a tenant key before exposing the API.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to build and test the charm.

## License

The Hindsight Operator is free software, distributed under the Apache Software License, version 2.0. See [LICENSE](LICENSE) for more information. Hindsight itself is Apache-2.0 licensed by Vectorize.
