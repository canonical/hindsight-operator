# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helpers for talking to a deployed hindsight unit."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import jubilant

# Runs in the *charm* container, which shares the pod network namespace with
# the workload, so localhost reaches the API and Control Plane. Shipping the
# script base64-encoded sidesteps all shell quoting.
_HTTP_SCRIPT = """
import json, sys, time, urllib.error, urllib.request
req = json.loads(sys.argv[1])
data = None if req["body"] is None else json.dumps(req["body"]).encode()
r = urllib.request.Request(req["url"], data=data, method=req["method"], headers=req["headers"])
for attempt in range(30):
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            out = {"status": resp.status, "body": resp.read().decode(errors="replace")}
        break
    except urllib.error.HTTPError as e:
        out = {"status": e.code, "body": e.read().decode(errors="replace")}
        break
    except urllib.error.URLError:
        # The API restarts on every config change; give it a moment.
        if attempt == 29:
            raise
        time.sleep(2)
print(json.dumps(out))
"""


@dataclass
class Response:
    status: int
    body: str

    def json(self):
        return json.loads(self.body)


def http(
    juju: jubilant.Juju,
    unit: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    port: int = 8888,
    token: str | None = None,
) -> Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = {
        "url": f"http://localhost:{port}{path}",
        "method": method,
        "body": body,
        "headers": headers,
    }
    script = base64.b64encode(_HTTP_SCRIPT.encode()).decode()
    arg = base64.b64encode(json.dumps(request).encode()).decode()
    task = juju.exec(
        f"python3 -c \"import base64;exec(base64.b64decode('{script}'))\" "
        f'"$(echo {arg} | base64 -d)"',
        unit=unit,
    )
    out = json.loads(task.stdout.strip().splitlines()[-1])
    return Response(status=out["status"], body=out["body"])


def wait_active(juju: jubilant.Juju) -> None:
    """Wait until every unit is active *and* idle.

    Checking idleness too avoids returning on the stale "active" status from
    before a config change, while its hooks have not run yet.
    """
    juju.wait(
        lambda s: jubilant.all_active(s) and jubilant.all_agents_idle(s),
        error=jubilant.any_error,
        delay=5,
        successes=4,
    )


def workload(juju: jubilant.Juju, unit: str, command: str) -> str:
    """Run a simple command in the hindsight workload container."""
    return juju.ssh(unit, command, container="hindsight")


def api_environment(juju: jubilant.Juju, unit: str) -> dict[str, str]:
    """The hindsight-api service environment from the live Pebble plan."""
    import yaml

    plan = yaml.safe_load(workload(juju, unit, "/charm/bin/pebble plan"))
    return plan["services"]["hindsight-api"]["environment"]


def retain_and_recall(juju: jubilant.Juju, unit: str, bank: str, token: str | None = None):
    fact = "The Hindsight charm integration test stores this sentence about zebras."
    retained = http(
        juju,
        unit,
        "POST",
        f"/v1/default/banks/{bank}/memories",
        {"items": [{"content": fact, "context": "integration test"}], "async": False},
        token=token,
    )
    assert retained.status == 200, retained.body

    recalled = http(
        juju,
        unit,
        "POST",
        f"/v1/default/banks/{bank}/memories/recall",
        {"query": "What does the integration test say about zebras?"},
        token=token,
    )
    assert recalled.status == 200, recalled.body
    assert "zebras" in recalled.body, recalled.body
