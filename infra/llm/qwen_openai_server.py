"""Serveur OpenAI-compatible minimal pour Qwen via Transformers.

Le serveur est volontairement autonome : il ne depend que de la bibliotheque
standard pour HTTP. PyTorch et Transformers ne sont importes qu'au demarrage
reel du moteur afin que le contrat HTTP puisse etre teste sans GPU.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any, Protocol
from uuid import uuid4


MAX_REQUEST_BYTES = 1_000_000


@dataclass(frozen=True)
class GenerationOutput:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"


class GenerationEngine(Protocol):
    model_name: str

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> GenerationOutput: ...


class TransformersEngine:
    """Charge une seule copie du modele et serialise les generations GPU."""

    def __init__(
        self,
        *,
        model_name: str,
        device: str,
        cache_dir: Path | None,
        dtype_name: str = "float16",
        attention: str = "sdpa",
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        dtypes = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }
        if dtype_name not in dtypes:
            raise ValueError(f"dtype non supporte: {dtype_name}")
        self.model_name = model_name
        self._torch = torch
        self._lock = threading.Lock()
        cache_value = str(cache_dir) if cache_dir is not None else None
        print(f"Chargement de {model_name} sur {device}...", flush=True)
        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            use_fast=True,
            cache_dir=cache_value,
        )
        load_kwargs: dict[str, Any] = {
            "dtype": dtypes[dtype_name],
            "attn_implementation": attention,
            "device_map": {"": device},
        }
        if cache_value:
            load_kwargs["cache_dir"] = cache_value
        self._model = AutoModelForCausalLM.from_pretrained(
            model_name,
            **load_kwargs,
        )
        self._model.eval()
        print(f"Modele {model_name} pret.", flush=True)

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> GenerationOutput:
        inputs = None
        generated = None
        completion_ids = None
        with self._lock, self._torch.inference_mode():
            try:
                rendered = self._tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                inputs = self._tokenizer(rendered, return_tensors="pt").to(
                    self._model.device
                )
                prompt_tokens = int(inputs.input_ids.shape[1])
                pad_token_id = (
                    self._tokenizer.pad_token_id or self._tokenizer.eos_token_id
                )
                generation: dict[str, Any] = {
                    "max_new_tokens": max_tokens,
                    "do_sample": temperature > 0,
                    "pad_token_id": pad_token_id,
                }
                if temperature > 0:
                    generation["temperature"] = temperature
                generated = self._model.generate(**inputs, **generation)
                completion_ids = generated[0, prompt_tokens:]
                completion_tokens = int(completion_ids.shape[0])
                text = self._tokenizer.decode(
                    completion_ids,
                    skip_special_tokens=True,
                ).strip()
            finally:
                del inputs, generated, completion_ids
                gc.collect()
                if self._torch.cuda.is_available():
                    self._torch.cuda.empty_cache()
        finish_reason = "length" if completion_tokens >= max_tokens else "stop"
        return GenerationOutput(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )


def _handler(engine: GenerationEngine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentPMSQwen/0.1"

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(
                status,
                {
                    "error": {
                        "message": message,
                        "type": "invalid_request_error"
                        if status.value < 500
                        else "server_error",
                    }
                },
            )

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(
                    HTTPStatus.OK,
                    {"status": "ready", "model": engine.model_name},
                )
                return
            if self.path == "/v1/models":
                self._json(
                    HTTPStatus.OK,
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": engine.model_name,
                                "object": "model",
                                "owned_by": "agentpms-local",
                            }
                        ],
                    },
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "Route inconnue.")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self._error(HTTPStatus.NOT_FOUND, "Route inconnue.")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "Taille de requete invalide.")
                return
            try:
                payload = json.loads(self.rfile.read(length))
                messages = payload.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise ValueError("messages est requis")
                clean_messages: list[dict[str, str]] = []
                for message in messages:
                    if not isinstance(message, dict):
                        raise ValueError("message invalide")
                    role = message.get("role")
                    content = message.get("content")
                    if role not in {"system", "user", "assistant"} or not isinstance(
                        content, str
                    ):
                        raise ValueError("message invalide")
                    clean_messages.append({"role": role, "content": content})
                max_tokens = int(payload.get("max_tokens", 800))
                temperature = float(payload.get("temperature", 0))
                if not 1 <= max_tokens <= 2_048:
                    raise ValueError("max_tokens doit etre compris entre 1 et 2048")
                if not 0 <= temperature <= 2:
                    raise ValueError("temperature doit etre comprise entre 0 et 2")
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
                return
            try:
                output = engine.generate(
                    clean_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as error:
                print(
                    f"Erreur de generation: {type(error).__name__}",
                    flush=True,
                )
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Generation impossible.")
                return
            created = int(time.time())
            self._json(
                HTTPStatus.OK,
                {
                    "id": "chatcmpl-" + uuid4().hex,
                    "object": "chat.completion",
                    "created": created,
                    "model": engine.model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": output.text,
                            },
                            "finish_reason": output.finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": output.prompt_tokens,
                        "completion_tokens": output.completion_tokens,
                        "total_tokens": output.prompt_tokens
                        + output.completion_tokens,
                    },
                },
            )

        def log_message(self, format: str, *args: Any) -> None:
            # Ne jamais journaliser le contenu des demandes hotelieres.
            print(f"{self.address_string()} - {format % args}", flush=True)

    return Handler


def build_server(
    engine: GenerationEngine,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _handler(engine))
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Service Qwen pour l'agent hotelier")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="Qwen/Qwen2.5-14B-Instruct")
    parser.add_argument("--device", default="cuda:1")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "huggingface",
    )
    parser.add_argument("--dtype", default="float16")
    parser.add_argument("--attention", default="sdpa")
    args = parser.parse_args()

    engine = TransformersEngine(
        model_name=args.model,
        device=args.device,
        cache_dir=args.cache_dir,
        dtype_name=args.dtype,
        attention=args.attention,
    )
    server = build_server(engine, host=args.host, port=args.port)
    print(f"Service Qwen disponible sur http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
