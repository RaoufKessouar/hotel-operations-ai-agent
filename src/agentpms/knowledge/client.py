"""Clients de la couche Knowledge. Le RAG historique reste autonome."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import sys
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentpms.contracts import Citation, KnowledgeResult


class KnowledgeClient(Protocol):
    def ask(
        self, question: str, history: list[dict[str, str]] | None = None
    ) -> KnowledgeResult: ...


class HTTPKnowledgeClient:
    """Client leger vers le gateway RAG local."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 120.0) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def ask(
        self, question: str, history: list[dict[str, str]] | None = None
    ) -> KnowledgeResult:
        body = json.dumps(
            {"question": question, "history": history or []},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_001)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError("Le service RAG local est indisponible.") from exc
        if len(raw) > 2_000_000:
            raise RuntimeError("La reponse du service RAG est trop volumineuse.")
        try:
            return KnowledgeResult.model_validate_json(raw)
        except ValueError as exc:
            raise RuntimeError("Le service RAG a retourne un format invalide.") from exc


class LocalRAGClient:
    """Adaptateur paresseux vers ``RAGpms/src.generation.answer.ask``.

    L'ajout du chemin est confine a cet adaptateur. Aucun fichier du RAG n'est
    copie et aucune logique de retrieval n'est reimplementee ici.
    """

    def __init__(self, project_root: Path, *, top_k: int = 5) -> None:
        self.project_root = project_root.resolve()
        self.top_k = top_k
        self._engine = None

    def _load_engine(self):
        if self._engine is not None:
            return self._engine
        expected = self.project_root / "src" / "generation" / "answer.py"
        if not expected.is_file():
            raise RuntimeError(f"Point d'entree RAG introuvable : {expected}")
        root_value = str(self.project_root)
        if root_value not in sys.path:
            sys.path.insert(0, root_value)
        module = importlib.import_module("src.generation.answer")
        origin = Path(module.__file__ or "").resolve()
        if self.project_root not in origin.parents:
            raise RuntimeError(
                "Le module 'src' charge ne correspond pas au projet RAG configure."
            )
        self._engine = module
        return module

    def ask(
        self, question: str, history: list[dict[str, str]] | None = None
    ) -> KnowledgeResult:
        result = self._load_engine().ask(
            question,
            top_k=self.top_k,
            hybrid=False,
            history=history or [],
        )
        cited_indices = {int(value) for value in re.findall(r"\[(\d+)\]", result.text)}
        citations: list[Citation] = []
        for index, hit in enumerate(result.hits, 1):
            if index not in cited_indices:
                continue
            document = hit.document
            details = [document.domain.upper(), document.source_type]
            if document.breadcrumb:
                details.append(document.breadcrumb)
            images = list(
                dict.fromkeys(
                    value
                    for value in [document.screenshot, *(document.screenshots or [])]
                    if value
                )
            )
            citations.append(
                Citation(
                    index=index,
                    title=document.title or document.source_id,
                    detail=" · ".join(details),
                    url=document.url,
                    score=float(hit.score),
                    timestamp_start=document.timestamp_start,
                    images=images,
                )
            )
        return KnowledgeResult(
            text=str(result.text),
            citations=citations,
            abstained=bool(result.abstained),
            route=str(result.route),
            citations_valid=bool(result.citations_valid),
            verification_decision=str(result.verification_decision),
        )
