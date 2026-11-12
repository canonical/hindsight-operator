# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Usage metrics on a live unit (003): request counts, and LLM calls per bank and client.

A stand-in OpenAI-compatible LLM in the charm container, which shares the pod's
network namespace, answers every chat completion with no facts and fixed token
counts. Hindsight records each call in its real ``llm_requests`` table, and the
leader's gateway totals the calls on the metrics port.
"""

from __future__ import annotations

import base64
import re

import jubilant
import pytest

from conftest import APP
from helpers import api_environment, http, wait_active

UNIT = f"{APP}/0"
ADMIN = "usage-admin-key"
KEYS = {"alice": "usage-key-alice"}
BANK_ACCESS = """
alice:
  banks: [alice-*]
"""
BANK = "alice-notes"
LLM_PORT = 18080
LLM_LOG = "/tmp/fake-llm.log"
PROMPT_TOKENS, COMPLETION_TOKENS = 11, 7
_LLM_SCRIPT = f"""
import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        with open({LLM_LOG!r}, "a") as log:
            log.write(self.path + "\\n")
        answer = {{
            "id": "chatcmpl-it", "object": "chat.completion", "created": 0,
            "model": json.loads(body or b"{{}}").get("model", "fake-model"),
            "choices": [{{"index": 0, "finish_reason": "stop",
                "message": {{"role": "assistant", "content": json.dumps({{"facts": []}})}}}}],
            "usage": {{"prompt_tokens": {PROMPT_TOKENS}, "completion_tokens": {COMPLETION_TOKENS},
                "total_tokens": {PROMPT_TOKENS + COMPLETION_TOKENS}}},
        }}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer.allow_reuse_address = True
http.server.ThreadingHTTPServer(("127.0.0.1", {LLM_PORT}), Handler).serve_forever()
"""


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    env = juju.add_secret("usage-env", {"tenant-api-key": ADMIN, "llm-api-key": "not-a-real-key"})
    keys = juju.add_secret("usage-access-keys", KEYS)
    juju.deploy(charm, APP, resources=resources, storage={"data": "1G"})
    for secret in (env, keys):
        juju.grant_secret(secret, APP)
    wait_active(juju)
    script = base64.b64encode(_LLM_SCRIPT.encode()).decode()
    juju.exec(
        f"setsid python3 -c \"import base64;exec(base64.b64decode('{script}'))\" "
        "</dev/null >/dev/null 2>&1 &",
        unit=UNIT,
    )
    juju.config(
        APP,
        {
            "env-secret": env,
            "access-keys": keys,
            "bank-access": BANK_ACCESS,
            "llm-provider": "openai",
            "llm-model": "fake-model",
            "llm-base-url": f"http://127.0.0.1:{LLM_PORT}/v1",
            "extra-env": "HINDSIGHT_API_SKIP_LLM_VERIFICATION=true",
        },
    )
    wait_active(juju)
    assert http(juju, UNIT, "PUT", f"/v1/default/banks/{BANK}", {}, token=ADMIN).status == 200


def _metrics(juju: jubilant.Juju) -> str:
    response = http(juju, UNIT, "GET", "/metrics", port=9464)
    assert response.status == 200, response.body
    return response.body


def _value(text: str, name: str, **labels: str) -> float | None:
    """The value of the series ``name`` whose labels include ``labels``."""
    for line in text.splitlines():
        match = re.fullmatch(rf"{name}(?:\{{(.*)\}})? (\S+)", line)
        if not match:
            continue
        found = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', match.group(1) or ""))
        if all(found.get(k) == v for k, v in labels.items()):
            return float(match.group(2))
    return None


def _retain(juju, items):
    return http(
        juju,
        UNIT,
        "POST",
        f"/v1/default/banks/{BANK}/memories",
        {"items": items, "async": False},
        token=KEYS["alice"],
    )


def test_hindsight_keeps_the_document_id_of_each_llm_call(juju: jubilant.Juju):
    """003 FR-008: the trace limit leaves room for the document ID."""
    env = api_environment(juju, UNIT)
    assert env["HINDSIGHT_API_LLM_TRACE_ENABLED"] == "true"
    assert env["HINDSIGHT_API_LLM_TRACE_MAX_CHARS"] == "256"


def test_requests_are_counted_per_client(juju: jubilant.Juju):
    """003 FR-001, FR-002."""
    assert (
        http(juju, UNIT, "GET", f"/v1/default/banks/{BANK}/profile", token=KEYS["alice"]).status
        == 200
    )
    assert http(juju, UNIT, "GET", "/v1/default/banks", token="guess").status == 401
    text = _metrics(juju)
    assert _value(
        text, "hindsight_gateway_requests_total", client="alice", bank=BANK, operation="read"
    ), text
    assert _value(text, "hindsight_gateway_requests_total", client="unauthenticated"), text
    assert "usage-key-alice" not in text and ADMIN not in text


def test_llm_calls_of_a_retain_are_totalled_under_its_client(juju: jubilant.Juju):
    """003 FR-005, FR-007, FR-018, FR-019, SC-006 on a live Hindsight."""
    response = _retain(juju, [{"content": "Alice likes zebras.", "document_id": "it-doc-1"}])
    assert response.status == 200, response.body
    response = _retain(juju, [{"content": "Alice also likes giraffes."}])
    assert response.status == 200, response.body
    llm_log = juju.exec(f"touch {LLM_LOG}; cat {LLM_LOG}", unit=UNIT).stdout
    assert "/chat/completions" in llm_log, llm_log

    def totalled(_status) -> bool:
        text = _metrics(juju)
        return all(
            _value(text, "hindsight_gateway_llm_calls_total", bank=BANK, client=client)
            for client in ("alice", "unattributed")
        )

    # The leader sweeps every 30 seconds.
    juju.wait(totalled, delay=10, timeout=3 * 60)
    text = _metrics(juju)
    assert _value(text, "hindsight_gateway_llm_usage_up") == 1
    labels = {"bank": BANK, "client": "alice", "operation": "retain", "model": "fake-model"}
    calls = _value(text, "hindsight_gateway_llm_calls_total", **labels)
    assert calls and calls >= 1, text
    assert _value(text, "hindsight_gateway_llm_tokens_total", **labels, type="input") == (
        calls * PROMPT_TOKENS
    )
    assert _value(text, "hindsight_gateway_llm_tokens_total", **labels, type="output") == (
        calls * COMPLETION_TOKENS
    )
