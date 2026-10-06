"""Compatibilite pour l'ancien wrapper de decision.

La comprehension du langage appartient desormais integralement au modele
principal. Les controles deterministes sont appliques apres sa decision par le
registre de tools, les permissions et la politique de grounding structurel.
"""

from __future__ import annotations

from agentpms.contracts import AgentDecision
from agentpms.tools.base import ToolSpec

from .decision import DecisionModel
from .state import AgentState


class GuardedDecisionModel:
    """Wrapper sans routage semantique, conserve pour compatibilite d'import."""

    def __init__(
        self,
        primary: DecisionModel,
        router: DecisionModel | None = None,
    ) -> None:
        self.primary = primary
        self.router = router

    def decide(self, state: AgentState, tools: list[ToolSpec]) -> AgentDecision:
        return self.primary.decide(state, tools)
