# Contributing

## Layout

| Path                         | Responsibility                                                    |
| ---------------------------- | ----------------------------------------------------------------- |
| `src/charm.py`               | `CharmBase` subclass: event wiring, holistic reconcile, status.   |
| `src/hindsight_env.py`       | Pure: config + secret + relation data → service environments.     |
| `src/workload.py`            | The Pebble layer (services, checks).                              |
| `src/defaults.py`            | Container layout (matches the upstream image) and constants.      |
| `lib/charms/…`               | Vendored charm libs: `data_platform_libs.v0.data_interfaces`, `traefik_k8s.v2.ingress`. |
| `tests/unit/`                | Fast tests: pure functions + `ops.testing` (Scenario) state transitions. |
| `tests/integration/`         | Jubilant tests that deploy to a real Kubernetes model.            |

## Unit tests and lint

```bash
tox -e format,lint,unit        # or: uvx --with tox-uv tox -e lint,unit
# without tox:
uv run --group unit pytest tests/unit
uv run --group lint ruff check src tests
```

## Integration tests

The integration tests create and destroy models. **They never use the
current controller implicitly.** You must set `JUJU_CONTROLLER`, and they
refuse JAAS or other shared controllers.

The supported way to run them is a throwaway LXD VM with its own
controller:

```bash
charmcraft pack
lxc launch ubuntu:24.04 hindsight-it --vm \
  -c limits.cpu=8 -c limits.memory=24GiB -d root,size=80GiB
lxc exec hindsight-it -- bash -c \
  'snap install concierge --classic && concierge prepare -p microk8s --extra-snaps astral-uv'
lxc file push -r . hindsight-it/root/hindsight-operator
lxc exec hindsight-it --cwd /root/hindsight-operator -- env \
  JUJU_CONTROLLER=concierge-microk8s \
  uv run --group integration pytest tests/integration -v -x
lxc delete --force hindsight-it   # when done
```

Environment knobs: `CHARM_PATH` (packed charm), `HINDSIGHT_IMAGE` (OCI image
override), `KEEP_MODELS=1` (keep models for debugging).

`test_embedded.py` covers the Workshop-SDK-equivalent setup with pg0.
`test_postgresql.py` deploys `postgresql-k8s` with pgvector and checks that
memory is shared across units.

## Conventions

- Runtime deps go in `pyproject.toml` and are locked in `uv.lock`
  (`uv lock` after changes). The charm builds with the charmcraft `uv` plugin.
- To update vendored libs, use `charmcraft fetch-libs` (declared under
  `charm-libs` in `charmcraft.yaml`), or download them from the libs'
  upstream repos.
- Don't commit built `*.charm` files.
