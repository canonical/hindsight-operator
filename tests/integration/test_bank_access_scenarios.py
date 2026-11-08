# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Per-key bank access as applications see it.

Two agents keep private memories, a team shares one bank, and an auditor may
read everything but change nothing. Each test is one step of that story, told
through what each key can actually recall.
"""

from __future__ import annotations

import jubilant
import pytest

from conftest import APP
from helpers import http, wait_active

UNIT = f"{APP}/0"
ADMIN = "story-admin-key"
KEYS = {
    "support-agent": "story-key-support",
    "billing-agent": "story-key-billing",
    "auditor": "story-key-auditor",
}
BANK_ACCESS = """
support-agent:
  banks: [support-*]
  read-banks: [team-handbook]
billing-agent:
  banks: [billing-*]
  read-banks: [team-handbook]
auditor:
  read-banks: ["*"]
"""

SUPPORT_FACT = "Customer 4711 prefers to be contacted by email about the zebra outage."
BILLING_FACT = "Invoice 9001 for the giraffe plan was refunded in full."
HANDBOOK_FACT = "Our team escalates any outage affecting penguins within one hour."


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    env = juju.add_secret("story-env", {"tenant-api-key": ADMIN})
    keys = juju.add_secret("story-access-keys", KEYS)
    juju.deploy(charm, APP, resources=resources, storage={"data": "1G"})
    for secret in (env, keys):
        juju.grant_secret(secret, APP)
    juju.config(APP, {"env-secret": env, "access-keys": keys, "bank-access": BANK_ACCESS})
    wait_active(juju)


def retain(juju, key, bank, text):
    return http(
        juju,
        UNIT,
        "POST",
        f"/v1/default/banks/{bank}/memories",
        {"items": [{"content": text, "context": "story"}], "async": False},
        token=key,
    )


def recall(juju, key, bank, query):
    return http(
        juju,
        UNIT,
        "POST",
        f"/v1/default/banks/{bank}/memories/recall",
        {"query": query},
        token=key,
    )


def recalled_text(response) -> str:
    assert response.status == 200, response.body
    return " ".join(r.get("text", "") for r in response.json().get("results", []))


def test_1_the_team_lead_writes_the_shared_handbook(juju: jubilant.Juju):
    """Only the admin key may write the bank everyone else reads."""
    assert retain(juju, ADMIN, "team-handbook", HANDBOOK_FACT).status == 200


def test_2_each_agent_keeps_private_memories(juju: jubilant.Juju):
    assert retain(juju, KEYS["support-agent"], "support-customers", SUPPORT_FACT).status == 200
    assert retain(juju, KEYS["billing-agent"], "billing-invoices", BILLING_FACT).status == 200


def test_3_an_agent_recalls_its_own_memories(juju: jubilant.Juju):
    text = recalled_text(recall(juju, KEYS["support-agent"], "support-customers", "zebra outage"))
    assert "zebra" in text


def test_4_agents_cannot_recall_each_others_memories(juju: jubilant.Juju):
    assert recall(juju, KEYS["support-agent"], "billing-invoices", "giraffe refund").status == 403
    assert recall(juju, KEYS["billing-agent"], "support-customers", "zebra outage").status == 403


def test_5_agents_do_not_even_see_each_others_banks(juju: jubilant.Juju):
    response = http(juju, UNIT, "GET", "/v1/default/banks", token=KEYS["support-agent"])
    banks = {b["bank_id"] for b in response.json()["banks"]}
    assert banks == {"support-customers", "team-handbook"}


def test_6_both_agents_read_the_handbook_but_cannot_edit_it(juju: jubilant.Juju):
    for agent in ("support-agent", "billing-agent"):
        text = recalled_text(recall(juju, KEYS[agent], "team-handbook", "penguin outage"))
        assert "penguins" in text
        edit = retain(juju, KEYS[agent], "team-handbook", "Escalations are optional.")
        assert edit.status == 403, edit.body


def test_7_the_auditor_reads_everything(juju: jubilant.Juju):
    for bank, query, word in (
        ("support-customers", "zebra outage", "zebra"),
        ("billing-invoices", "giraffe refund", "giraffe"),
        ("team-handbook", "penguin outage", "penguins"),
    ):
        assert word in recalled_text(recall(juju, KEYS["auditor"], bank, query))


def test_8_the_auditor_changes_nothing(juju: jubilant.Juju):
    assert retain(juju, KEYS["auditor"], "support-customers", "Tampered.").status == 403
    delete = http(
        juju, UNIT, "DELETE", "/v1/default/banks/billing-invoices", token=KEYS["auditor"]
    )
    assert delete.status == 403
    # What the agents stored is still there.
    text = recalled_text(recall(juju, KEYS["billing-agent"], "billing-invoices", "giraffe refund"))
    assert "giraffe" in text and "Tampered" not in text
