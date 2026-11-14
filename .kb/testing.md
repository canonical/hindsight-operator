# Preface

How the charm is tested: the unit suite, the integration suite, and the controllers the integration suite may run against. Read this before running or changing a test.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Never a shared controller

Integration tests create and destroy models. Run them only on a controller you bootstrapped for the purpose, named in `JUJU_CONTROLLER`. Never run them, or deploy anything, on JAAS or on the current controller by default. `tests/integration/conftest.py` refuses a controller with no cloud or with `jaas` in its name, and that check must not be weakened.

## §2 - Tests travel with behavior

A change to behavior adds or updates the tests that show it in the same commit. Unit tests cite the `FR-*` or `SC-*` identifier they verify when the behavior has one.

## §3 - Lint and unit before review

Run `tox -e lint,unit` before opening or updating a pull request. CI runs the same environments.


# Concepts

## Unit tests

`tests/unit/` uses `ops.testing` (Scenario) for state transitions in `test_charm.py` and plain pytest for the pure functions in `test_hindsight_env.py`. The gateway's tests run it in front of a fake Hindsight API: `test_gateway_members.py` signs member tokens with a fake OpenID Connect issuer, and `test_gateway_grants.py` keeps grants in a real PostgreSQL that `pg0-embedded` starts, as the image's embedded database does. `test_gateway_usage.py` reads the request counts from the metrics port, and sweeps a real `llm_requests` table in the same PostgreSQL. `test_gateway_spans.py` checks the trace headers Hindsight receives, and the spans exported to an in-memory exporter and to a stand-in OTLP endpoint. `test_gateway_attribution.py` checks the documents recorded per retain, the sweep's join against `llm_requests` records carrying `metadata.document_id`, and the spans of the attributed calls. The PostgreSQL fixtures are in `tests/unit/conftest.py`.

```bash
tox -e format,lint,unit
# without tox:
uv run --group unit pytest tests/unit
uv run --group lint ruff check src tests
```

## Integration tests

`tests/integration/` uses Jubilant against a real Kubernetes model.

- `test_embedded.py`: a single unit on pg0.
- `test_postgresql.py`: `postgresql-k8s` with pgvector, with memory shared across units.
- `test_proxy.py`: LLM traffic follows the model's proxy settings, using a fake key. Nothing leaves the pod.
- `test_bank_access.py`: per-key bank access on a live unit: the gateway in front of the API, isolation, read-only keys, admin-only routes, raw-path traversal, MCP, and turning the feature off.
- `test_bank_access_scenarios.py`: per-key bank access as a story of two agents and an auditor storing and recalling real memories.
- `test_usage_metrics.py`: usage metrics on a live unit. A stand-in OpenAI-compatible LLM in the pod answers Hindsight, and the metrics port shows the requests per client and the LLM calls of a retain with a `document_id` totalled under its client, and of one without as `unattributed`.
- `test_published.py`: deploys a Charmhub release. Skipped unless `CHARM_CHANNEL` is set. `EXPECTED_CHARM_REVISION` pins the revision.

Environment variables: `JUJU_CONTROLLER` (required), `CHARM_PATH`, `HINDSIGHT_IMAGE`, `KEEP_MODELS=1`.

## Local controller

The supported local setup is a throwaway LXD VM with its own MicroK8s controller, prepared by concierge with the same `concierge.yaml` that CI uses. `CONTRIBUTING.md` has the commands. The upstream image is large, so give the VM at least 8 CPUs, 24 GiB of memory and 80 GiB of disk.

## Spread

`spread.yaml` and `spread/multitenancy/` demonstrate per-key bank access as one task per scenario, each a `curl` script that prints an `ok:` line per check. The suite's setup bootstraps Juju on MicroK8s with the same `concierge.yaml` as CI when no controller exists, and deploys the charm named by `CRAFT_ARTIFACT`. `spread/lib/hindsight.sh` sends paths with `curl --path-as-is`, since most HTTP clients normalise `..` before sending.

## CI

`.github/workflows/ci.yaml` runs lint and unit tests, packs the charm on amd64 and arm64, and runs `test_embedded.py`, `test_proxy.py`, `test_bank_access.py`, `test_bank_access_scenarios.py` and `test_usage_metrics.py` on a MicroK8s controller that `.github/actions/setup-k8s` bootstraps on each runner. The `spread` job runs `charmcraft test` on an amd64 runner, which packs the charm and, with `CI` set, runs the suite on the runner itself. `test_postgresql.py` is not run in CI.
