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
juju deploy hindsight --channel latest/edge
```

The embedded database lives on the `data` storage volume and supports one unit. Data is not migrated between the embedded and an external database.

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

To scale beyond one unit, relate to PostgreSQL. Hindsight needs pgvector, which must be enabled on the PostgreSQL charm:

```shell
juju deploy postgresql-k8s --channel 14/stable --trust \
  --config plugin_vector_enable=true    # 16 track: plugin-vector-enable=true
juju config hindsight allow-embedded-database=false
juju integrate hindsight postgresql-k8s
juju add-unit hindsight -n 2
```

For a PostgreSQL that Juju doesn't manage, set `database-url=postgresql://...` in `env-secret` instead.

If the relation is later removed, the charm blocks rather than switching to an empty embedded database. Relate again, or run `juju run hindsight/0 use-embedded-database` to start over on the embedded database.

### Ingress

To expose the API, for example through Traefik:

```shell
juju integrate hindsight traefik-k8s
```

The Control Plane cannot be served under a path prefix. Reach it with `kubectl port-forward svc/hindsight 9999 -n <model>`.

## Security

Without `tenant-api-key`, the API accepts unauthenticated requests from anything that can reach it. Set a tenant key before exposing the API.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to build and test the charm.

## License

The Hindsight Operator is free software, distributed under the Apache Software License, version 2.0. See [LICENSE](LICENSE) for more information. Hindsight itself is Apache-2.0 licensed by Vectorize.
