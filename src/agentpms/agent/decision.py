"""Abstraction du modele qui choisit la prochaine etape."""

from __future__ import annotations

from collections import deque
from typing import Protocol

from agentpms.contracts import AgentDecision
from agentpms.tools.base import ToolSpec

from .state import AgentState


class DecisionModelError(RuntimeError):
    """Erreur explicite du moteur de decision."""


class DecisionModelUnavailableError(DecisionModelError):
    """Le service du modele n'a pas pu produire de reponse."""


class InvalidDecisionError(DecisionModelError):
    """Le service a repondu, mais aucune decision valide n'a pu etre lue."""


class DecisionModel(Protocol):
    def decide(self, state: AgentState, tools: list[ToolSpec]) -> AgentDecision: ...


class QueuedDecisionModel:
    """Modele deterministe pour tester le workflow sans simuler un endpoint LLM."""

    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = deque(decisions)

    def decide(self, state: AgentState, tools: list[ToolSpec]) -> AgentDecision:
        if not self._decisions:
            raise RuntimeError("Aucune decision de test restante")
        return self._decisions.popleft()
