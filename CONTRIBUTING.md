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
| `requirements/`              | Committed feature requirements, one `NNN-slug/requirements.md` each. |
| `scripts/`                   | Repository tooling: requirements adoption and checks.             |
| `AGENTS.md`, `.kb/`          | Knowledge base for coding agents.                                 |

## Feature delivery

New features are defined with [Spec Kit](https://github.com/github/spec-kit) and delivered as a stack of pull requests:

1. Run `/speckit-specify`, `/speckit-clarify`, `/speckit-plan` and `/speckit-tasks`. Their output under `specs/` is gitignored.
2. Move the requirements into place with `tox -e adopt-requirements -- NNN-slug`, rewrite them into the format in [`requirements/.kb/requirements.md`](requirements/.kb/requirements.md), and run `tox -e check-requirements`.
3. Open a base pull request against `main` with the requirements and documentation only.
4. Stack one implementation pull request per deliverable on top of it, each with its tests.

Only `requirements/NNN-slug/requirements.md` is committed from a Spec Kit run. The `conventions` workflow fails a pull request that tracks a `spec.md`, anything under `specs/`, or any file besides `requirements.md` in a feature directory. [`.kb/spec-kit.md`](.kb/spec-kit.md) and [`.kb/pull-requests.md`](.kb/pull-requests.md) have the details.

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
lxc file push -r . hindsight-it/root/hindsight-operator
lxc exec hindsight-it -- bash -c \
  'snap install concierge --classic && concierge prepare -c /root/hindsight-operator/.github/actions/setup-k8s/concierge.yaml'
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

`test_proxy.py` checks with a fake key that LLM traffic follows the model's
proxy settings; nothing leaves the pod.

`test_bank_access.py` checks per-key bank access on a live unit, and
`test_bank_access_scenarios.py` tells it as a story of two agents and an
auditor recalling real memories.

`test_published.py` deploys a Charmhub release instead of a packed charm. It
is skipped unless `CHARM_CHANNEL` is set; `EXPECTED_CHARM_REVISION` optionally
pins the revision.

## Spread

`spread/multitenancy/` demonstrates per-key bank access as a sequence of `curl` tasks against a live deployment, one task per scenario. Run it with `charmcraft test`, which packs the charm and runs the suite in a new LXD VM. With `CI` set, it runs the suite on the machine itself, as the `spread` CI job does on amd64.

## Publishing

CI does this. Pull requests lint, unit test, build and run `test_embedded.py`
and `test_proxy.py` on native amd64 and arm64 runners. Pushes to `main` then also upload both charms
and the image named by `upstream-source`, release them to `latest/edge`, and
run `test_published.py` against that release on both architectures.

To try a branch before merging, publish it to a Charmhub branch, which expires
30 days after its last release:

```bash
gh workflow run CI --ref <git-branch> -f charmhub-branch=pr-<N>
juju deploy hindsight --channel latest/edge/pr-<N> \
  --base ubuntu@24.04 --constraints arch=amd64     # Juju needs both for a branch
juju refresh hindsight --channel latest/edge/pr-<N>  # an existing app has both
```

Publishing uses the `CHARMHUB_TOKEN` repository secret, created with
`charmcraft login --export` and limited to this charm. Release to other channels
by hand with `charmcraft release`.

## Conventions

- Runtime deps go in `pyproject.toml` and are locked in `uv.lock`
  (`uv lock` after changes). The charm builds with the charmcraft `uv` plugin.
- To update vendored libs, use `charmcraft fetch-libs` (declared under
  `charm-libs` in `charmcraft.yaml`), or download them from the libs'
  upstream repos.
- Don't commit built `*.charm` files.
