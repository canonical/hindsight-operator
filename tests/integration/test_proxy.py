# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Check that Hindsight's LLM traffic follows the Juju model's proxy settings.

A small proxy in the charm container, which shares the pod's network namespace,
records each CONNECT and refuses it. The API key is fake and nothing leaves the
pod, so this shows which host each provider tries to reach, not that the provider
works.
"""

from __future__ import annotations

import base64
import time

import jubilant
import pytest

from conftest import APP
from helpers import api_environment, http, wait_active

UNIT = f"{APP}/0"
PROXY_PORT = 3128
PROXY_LOG = "/tmp/proxy-connects.log"
_PROXY_SCRIPT = f"""
import socketserver
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode(errors="replace").strip()
        with open({PROXY_LOG!r}, "a") as log:
            log.write(line + "\\n")
        self.wfile.write(b"HTTP/1.1 403 Forbidden\\r\\nContent-Length: 0\\r\\n\\r\\n")
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("127.0.0.1", {PROXY_PORT}), Handler).serve_forever()
"""


def _proxy_log(juju: jubilant.Juju) -> str:
    return juju.exec(f"touch {PROXY_LOG}; cat {PROXY_LOG}", unit=UNIT).stdout


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju, charm, resources):
    juju.deploy(charm, APP, resources=resources)
    wait_active(juju)
    script = base64.b64encode(_PROXY_SCRIPT.encode()).decode()
    juju.exec(
        f"setsid python3 -c \"import base64;exec(base64.b64decode('{script}'))\" "
        "</dev/null >/dev/null 2>&1 &",
        unit=UNIT,
    )
    juju.cli("model-config", f"juju-https-proxy=http://127.0.0.1:{PROXY_PORT}")
    # Juju fires no hook for this: update-status, every minute here, must apply it.
    juju.wait(
        lambda _: api_environment(juju, UNIT).get("HTTPS_PROXY", "").endswith(f":{PROXY_PORT}"),
        delay=10,
        timeout=5 * 60,
    )
    wait_active(juju)


def test_model_proxy_reaches_the_api_and_keeps_local_traffic_direct(juju: jubilant.Juju):
    env = api_environment(juju, UNIT)
    assert env["https_proxy"] == f"http://127.0.0.1:{PROXY_PORT}"
    assert "localhost" in env["NO_PROXY"]
    response = http(juju, UNIT, "GET", "/health")
    assert response.status == 200, response.body
    # The image bundles these: startup mustn't need them through a proxy.
    log = _proxy_log(juju)
    assert "huggingface.co" not in log, log
    assert "raw.githubusercontent.com" not in log, log


@pytest.mark.parametrize(
    ("provider", "model", "host"),
    [
        ("gemini", "gemini-2.5-flash", "generativelanguage.googleapis.com"),
        ("openrouter", "openai/gpt-4o-mini", "openrouter.ai"),
    ],
)
def test_llm_provider_connects_through_the_proxy(juju: jubilant.Juju, provider, model, host):
    juju.config(
        APP,
        {
            "llm-provider": provider,
            "llm-model": model,
            "extra-env": "HINDSIGHT_API_LLM_API_KEY=not-a-real-key\n"
            "HINDSIGHT_API_SKIP_LLM_VERIFICATION=true",
        },
    )
    deadline = time.monotonic() + 8 * 60
    while time.monotonic() < deadline:
        if f"CONNECT {host}:443" in _proxy_log(juju):
            return
        # Startup may already have checked the provider. If not, retain calls it.
        try:
            http(
                juju,
                UNIT,
                "POST",
                f"/v1/default/banks/proxy-{provider}/memories",
                {"items": [{"content": "A sentence about zebras."}], "async": True},
            )
        except jubilant.TaskError:
            pass  # The API restarts on a config change.
        time.sleep(15)
    pytest.fail(f"no CONNECT to {host} seen; proxy log:\n{_proxy_log(juju)}")
