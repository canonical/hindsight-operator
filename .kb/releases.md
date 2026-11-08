# Preface

How the charm and its image reach Charmhub, and how a release is verified. Read this before changing `.github/workflows/ci.yaml`, `upstream-source` in `charmcraft.yaml`, or the release process.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - CI publishes edge

Only the `publish` job on a push to `main` uploads and releases to `latest/edge`. Release to other channels by hand with `charmcraft release`.

## §2 - Charms before resource

Charmhub accepts a resource only when an uploaded revision declares it, so the job uploads both charms before the image.

## §3 - Re-runs reuse revisions

A re-run of `publish` uploads the same files again. Charmhub refuses a duplicate and names the existing revision, and the job releases that revision. Keep that behavior when changing the upload step.


# Concepts

## Pipeline

1. `lint-unit`, `build` (amd64 and arm64), `integration` and `spread` run on every pull request and push.
2. On a push to `main`, `publish` uploads `hindsight_amd64.charm` and `hindsight_arm64.charm`, uploads the multi-arch image named by `upstream-source` as one `hindsight-image` resource revision, and releases both charm revisions to `latest/edge` with it.
3. `verify-published` deploys the release from Charmhub on both architectures and runs `test_published.py` against the revision `publish` reported.

## Credentials

`publish` authenticates with the `CHARMHUB_TOKEN` repository secret, created with `charmcraft login --export` and limited to this charm.

## Dependencies

Renovate (`renovate.json`) proposes dependency updates. Runtime dependencies are declared in `pyproject.toml` and locked in `uv.lock`. Run `uv lock` after changing them. The charm builds with the charmcraft `uv` plugin.
