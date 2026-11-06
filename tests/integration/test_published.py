# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Check a Charmhub release: the charm and its image, as a user would deploy them.

Skipped unless ``CHARM_CHANNEL`` is set. ``EXPECTED_CHARM_REVISION`` optionally
pins the revision the channel must serve on this architecture.
"""

from __future__ import annotations

import os

import jubilant
import pytest

from conftest import APP, ARCH, METADATA
from helpers import http, retain_and_recall, wait_active

CHANNEL = os.environ.get("CHARM_CHANNEL", "")
EXPECTED_REVISION = os.environ.get("EXPECTED_CHARM_REVISION", "")
UNIT = f"{APP}/0"

pytestmark = pytest.mark.skipif(not CHANNEL, reason="CHARM_CHANNEL is not set")


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju):
    # No resources are passed, so the image can only come from Charmhub.
    # Juju can't pick a release from a branch channel (track/risk/branch) by
    # itself: it needs the base and architecture given explicitly.
    juju.deploy(APP, channel=CHANNEL, base=METADATA["base"], constraints={"arch": ARCH})
    wait_active(juju)


def test_channel_serves_expected_revision(juju: jubilant.Juju):
    app = juju.status().apps[APP]
    assert app.charm_channel == CHANNEL
    if EXPECTED_REVISION:
        assert app.charm_rev == int(EXPECTED_REVISION)


def test_api_health(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/health")
    assert response.status == 200, response.body


def test_control_plane_serves(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/", port=9999)
    assert response.status < 500, response.body


def test_retain_and_recall(juju: jubilant.Juju):
    retain_and_recall(juju, UNIT, bank="published-it")
