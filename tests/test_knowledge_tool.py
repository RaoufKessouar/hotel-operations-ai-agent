from __future__ import annotations

from agentpms.contracts import Citation, KnowledgeResult, ToolCall, ToolStatus
from agentpms.tools.base import ToolContext
from agentpms.tools.knowledge_tools import SearchKnowledgeTool


class FakeKnowledgeClient:
    def ask(self, question, history=None):
        return KnowledgeResult(
            text="Le check-out est a 11 h [1].",
            citations=[Citation(index=1, title="Procedure check-out")],
            citations_valid=True,
            verification_decision="supported",
            route="hotel",
        )


def test_knowledge_tool_preserves_citations():
    tool = SearchKnowledgeTool(FakeKnowledgeClient())
    call = ToolCall(
        name=tool.name,
        arguments={"question": "A quelle heure est le check-out ?"},
    )
    result = tool.execute(
        tool.input_model.model_validate(call.arguments),
        ToolContext(
            request_id="request",
            call_id=call.call_id,
            actor_id="user",
            role="reception",
        ),
    )

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert result.citations[0].title == "Procedure check-out"

