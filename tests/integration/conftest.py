# Copyright 2026 Matias Piipari
# See LICENSE file for licensing details.
"""Fixtures for the hindsight integration tests (Jubilant).

Safety: these tests create and destroy models. They NEVER fall back to the
current Juju controller. Set ``JUJU_CONTROLLER`` to a controller you own on a
Kubernetes cloud (e.g. a local Canonical K8s / MicroK8s bootstrap); JAAS and
other shared controllers are refused.

Environment:
    JUJU_CONTROLLER  required; controller to create temporary models on
    CHARM_PATH       optional; path to a packed .charm (default: ./hindsight_*.charm)
    HINDSIGHT_IMAGE  optional; OCI image for the hindsight-image resource
    KEEP_MODELS      optional; "1" to keep models after the run for debugging
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from collections.abc import Iterator
from pathlib import Path

import jubilant
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
METADATA = yaml.safe_load((ROOT / "charmcraft.yaml").read_text())
APP = METADATA["name"]
WAIT_TIMEOUT = 30 * 60  # the upstream image is large (bundled ML models)
# Juju schedules K8s pods with an amd64 node selector unless told otherwise.
ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


def _assert_safe_controller(name: str) -> None:
    """Refuse shared controllers. JAAS controllers report no cloud."""
    raw = subprocess.run(
        ["juju", "controllers", "--format=json"], check=True, capture_output=True, text=True
    ).stdout
    controllers = json.loads(raw).get("controllers", {})
    if name not in controllers:
        pytest.exit(f"JUJU_CONTROLLER={name!r} is not a known controller", returncode=2)
    details = controllers[name]
    if "jaas" in name.lower() or not details.get("cloud"):
        pytest.exit(
            f"Refusing to run integration tests on {name!r}: it looks like a shared JAAS "
            "controller. Bootstrap a local Kubernetes controller and point JUJU_CONTROLLER at it.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def controller() -> str:
    name = os.environ.get("JUJU_CONTROLLER", "").strip()
    if not name:
        pytest.exit(
            "Set JUJU_CONTROLLER to a local Kubernetes controller you own; "
            "the integration tests never use the current controller implicitly.",
            returncode=2,
        )
    _assert_safe_controller(name)
    return name


@pytest.fixture(scope="module")
def juju(controller: str, request: pytest.FixtureRequest) -> Iterator[jubilant.Juju]:
    keep = os.environ.get("KEEP_MODELS", "") == "1"
    # A short update-status interval keeps unit status fresh between hooks.
    config = {"update-status-hook-interval": "1m"}
    with jubilant.temp_model(keep=keep, controller=controller, config=config) as juju:
        juju.wait_timeout = WAIT_TIMEOUT
        juju.cli("set-model-constraints", f"arch={ARCH}")
        yield juju
        if request.session.testsfailed:
            print(juju.debug_log(limit=300), end="")


@pytest.fixture(scope="session")
def charm() -> Path:
    if path := os.environ.get("CHARM_PATH"):
        return Path(path).resolve()
    packed = sorted(ROOT.glob(f"{APP}_*.charm"))
    if not packed:
        pytest.exit("No packed charm found: run `charmcraft pack` or set CHARM_PATH", returncode=2)
    return packed[-1]


@pytest.fixture(scope="session")
def resources() -> dict[str, str]:
    upstream = METADATA["resources"]["hindsight-image"]["upstream-source"]
    return {"hindsight-image": os.environ.get("HINDSIGHT_IMAGE", upstream)}
