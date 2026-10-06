from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MODULE_PATH = (
    Path(__file__).parents[1] / "infra" / "llm" / "qwen_openai_server.py"
)
SPEC = importlib.util.spec_from_file_location("qwen_openai_server", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SERVER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER_MODULE
SPEC.loader.exec_module(SERVER_MODULE)


class FakeEngine:
    model_name = "Qwen/Qwen2.5-14B-Instruct"

    def generate(self, messages, *, max_tokens, temperature):
        assert messages[-1] == {"role": "user", "content": "Bonjour"}
        assert max_tokens == 25
        assert temperature == 0
        return SERVER_MODULE.GenerationOutput(
            text='{"kind":"final","response":"Bonjour"}',
            prompt_tokens=10,
            completion_tokens=8,
        )


def _running_server():
    server = SERVER_MODULE.build_server(FakeEngine(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_qwen_server_exposes_health_and_model_contract():
    server, base_url = _running_server()
    try:
        with urlopen(base_url + "/health", timeout=2) as response:
            health = json.load(response)
        with urlopen(base_url + "/v1/models", timeout=2) as response:
            models = json.load(response)
    finally:
        server.shutdown()
        server.server_close()

    assert health == {
        "status": "ready",
        "model": "Qwen/Qwen2.5-14B-Instruct",
    }
    assert models["data"][0]["id"] == "Qwen/Qwen2.5-14B-Instruct"


def test_qwen_server_returns_an_openai_compatible_completion():
    server, base_url = _running_server()
    request = Request(
        base_url + "/v1/chat/completions",
        data=json.dumps(
            {
                "model": "Qwen/Qwen2.5-14B-Instruct",
                "messages": [{"role": "user", "content": "Bonjour"}],
                "temperature": 0,
                "max_tokens": 25,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
    finally:
        server.shutdown()
        server.server_close()

    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"].startswith("{")
    assert payload["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 8,
        "total_tokens": 18,
    }


def test_qwen_server_rejects_an_invalid_request():
    server, base_url = _running_server()
    request = Request(
        base_url + "/v1/chat/completions",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        try:
            urlopen(request, timeout=2)
            raise AssertionError("La requete invalide aurait du etre rejetee")
        except HTTPError as error:
            payload = json.load(error)
            assert error.code == 400
            assert payload["error"]["type"] == "invalid_request_error"
    finally:
        server.shutdown()
        server.server_close()
