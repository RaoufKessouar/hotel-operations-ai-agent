"""Gateway HTTP local vers le RAG historique, sans modifier RAGpms."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from agentpms.contracts import KnowledgeResult

from .client import KnowledgeClient, LocalRAGClient


MAX_REQUEST_BYTES = 64_000


class KnowledgeGateway:
    def __init__(self, client: KnowledgeClient) -> None:
        self.client = client

    def query(self, payload: dict[str, Any]) -> KnowledgeResult:
        question = payload.get("question")
        history = payload.get("history", [])
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question est requise")
        if not isinstance(history, list) or len(history) > 12:
            raise ValueError("history doit contenir au maximum 12 messages")
        clean_history = []
        for item in history:
            if not isinstance(item, dict):
                raise ValueError("message historique invalide")
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("message historique invalide")
            clean_history.append({"role": role, "content": content[:8_000]})
        return self.client.ask(question.strip()[:8_000], history=clean_history)


def _handler(gateway: KnowledgeGateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AGENTpmsKnowledge/0.1"

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
            if self.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - API de BaseHTTPRequestHandler
            if self.path != "/knowledge/query":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("objet JSON attendu")
                result = gateway.query(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return
            except Exception:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "rag_unavailable"}
                )
                return
            self._json(HTTPStatus.OK, result.model_dump(mode="json"))

        def log_message(self, format: str, *args: Any) -> None:
            # Ne pas journaliser les requetes potentiellement sensibles ici.
            return None

    return Handler


def serve(
    client: KnowledgeClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = ThreadingHTTPServer((host, port), _handler(KnowledgeGateway(client)))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gateway local vers RAGpms")
    parser.add_argument("--rag-project", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(
        LocalRAGClient(args.rag_project),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
