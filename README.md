# Hindsight (Kubernetes charm)

A [Juju](https://juju.is) Kubernetes charm, written with the
[ops](https://ops.readthedocs.io) framework, that operates
[Hindsight](https://github.com/vectorize-io/hindsight) — Vectorize's
agent-memory system that retains, recalls, and reflects on world facts,
experiences, and mental models.

It is the Kubernetes counterpart of the
[Hindsight Workshop SDK](https://github.com/mz2/hindsight-sdk) and keeps the
same shape and defaults:

| Workshop SDK                                   | This charm                                              |
| ---------------------------------------------- | ------------------------------------------------------- |
| `hindsight-api` systemd user service (:8888)    | `hindsight-api` Pebble service (:8888)                   |
| `hindsight-control-plane` service (:9999)       | `hindsight-control-plane` Pebble service (:9999)         |
| `~/.hindsight/server.env` (LLM provider)        | `llm-*` config options + `env-secret` Juju secret        |
| `HINDSIGHT_API_LLM_PROVIDER=none` default       | `llm-provider=none` default                              |
| `hindsight-pg-embedded` mount (`~/.pg0`)        | `data` storage at `/home/hindsight/.pg0`                 |
| `llm-endpoint` tunnel to a host Ollama          | `llm-base-url`, e.g. an in-cluster Ollama Service        |
| `hindsight-api` / `-control-plane` tunnel slots | Kubernetes Service ports, `ingress` relation for the API |
| `check-health` hook (`/health`)                 | Pebble `ready` check on `/health` → unit status          |

On top of that, the charm can use an external PostgreSQL over the `database`
relation, which is what makes it scale beyond one unit.

> Hindsight is Apache-2.0 licensed by Vectorize. This charm is an independent
> packaging effort, also Apache-2.0.

## Requirements

- Juju 3.3+ on a Kubernetes cloud (Canonical K8s, MicroK8s, EKS, …).
- The workload image defaults to the upstream standalone image
  `ghcr.io/vectorize-io/hindsight` (amd64 and arm64). It is large because it
  bundles local embedding/reranker models.

## Quick start (embedded database)

```bash
charmcraft pack
juju deploy ./hindsight_arm64.charm hindsight \
  --resource hindsight-image=ghcr.io/vectorize-io/hindsight:0.9.2
juju status --watch 5s   # hindsight/0 ... active  embedded database
```

With no `database` relation, Hindsight runs its embedded PostgreSQL (pg0) on
the `data` storage volume, like the Workshop SDK. That mode is limited to a
**single unit**, because each pod would otherwise get its own separate memory.
If you add a unit, the unit whose volume already holds the memories keeps
serving (status `embedded database; other units blocked`), and the new units
block until you relate PostgreSQL or scale back down.

## Production: PostgreSQL with pgvector

Hindsight needs the `vector` extension, and a relation user is not allowed to
create it. Enable the plugin on the PostgreSQL charm first:

```bash
juju deploy postgresql-k8s --channel 14/stable --trust \
  --config plugin_vector_enable=true      # 16/edge: plugin-vector-enable=true
juju deploy ./hindsight_arm64.charm hindsight \
  --resource hindsight-image=ghcr.io/vectorize-io/hindsight:0.9.2 \
  --config allow-embedded-database=false
juju integrate hindsight postgresql-k8s
juju add-unit hindsight -n 2               # units share the same memory
```

Data is **not** migrated when moving between embedded and external databases.
Once a unit has used the external database, removing the `database` relation
blocks it (`database relation removed: …`) instead of silently switching to an
empty pg0. Relate PostgreSQL again, or, if you really want to start over on
the embedded database, run:

```bash
juju run hindsight/0 use-embedded-database
```

To use a PostgreSQL that Juju doesn't manage (for example RDS or Cloud SQL),
put `database-url=postgresql://…` in `env-secret` instead of relating. The
`database` relation always takes precedence over that URL.

## Configuring an LLM

By default `llm-provider=none`. Retain and recall still work, but LLM-only
operations (reflect, mental-model refresh) are disabled. That is the right
mode when a client such as Hermes owns the conversational LLM and uses
Hindsight only as its memory backend.

Cloud provider, with the key held in a Juju secret:

```bash
juju add-secret hindsight-keys llm-api-key=sk-...
juju grant-secret hindsight-keys hindsight
juju config hindsight llm-provider=openai llm-model=gpt-4o-mini \
  env-secret=secret:<id-returned-above>
```

Keyless provider, using an Ollama running in the cluster:

```bash
juju config hindsight llm-provider=ollama llm-model=gemma3:12b \
  llm-base-url=http://ollama.ollama.svc.cluster.local:11434/v1
```

Any other Hindsight setting can be set with `extra-env` (non-secret
`KEY=VALUE` lines) or as a key in `env-secret`.

### Secret key mapping

Secret keys are upper-cased and dashes become underscores. Keys that don't
already start with `hindsight-` also get the `HINDSIGHT_API_` prefix.

| Secret key                | Becomes                          | Effect                                                     |
| ------------------------- | -------------------------------- | ---------------------------------------------------------- |
| `llm-api-key`             | `HINDSIGHT_API_LLM_API_KEY`      | Provider API key                                           |
| `tenant-api-key`          | `HINDSIGHT_API_TENANT_API_KEY`   | Turns on API-key auth (`Authorization: Bearer …`); the Control Plane gets the key automatically |
| `hindsight-cp-access-key` | `HINDSIGHT_CP_ACCESS_KEY`        | Puts the Control Plane UI behind an access key             |
| `database-url`            | `HINDSIGHT_API_DATABASE_URL`     | External DB that Juju doesn't manage                       |

> **Security:** without `tenant-api-key`, the API accepts unauthenticated
> requests from anything that can reach the Service. Set a tenant key before
> exposing the API through ingress.

## Reaching the services

- **API:** relate to an ingress provider such as `traefik-k8s`
  (`juju integrate hindsight traefik-k8s`). The API is served with the path
  prefix stripped.
- **Control Plane:** a Next.js app whose base path is fixed when the image is
  built, so it doesn't work behind path-prefix ingress. Use
  `kubectl port-forward svc/hindsight 9999 -n <model>`, or a dedicated host.

## Actions

| Action                  | Purpose                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `use-embedded-database` | Opt back into pg0 after the `database` relation was removed          |

## Relations

| Endpoint   | Interface           | Purpose                                          |
| ---------- | ------------------- | ------------------------------------------------ |
| `database` | `postgresql_client` | External PostgreSQL (needs pgvector)             |
| `ingress`  | `ingress`           | Exposes the API (:8888) through e.g. traefik-k8s |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
