"""Assemblage de l'application locale lancee par Uvicorn."""

from pathlib import Path

from agentpms.agent import LocalRulesDecisionModel
from agentpms.config import Settings
from agentpms.knowledge import HTTPKnowledgeClient
from agentpms.llm import DeepSeekDecisionModel, OpenAICompatibleDecisionModel
from agentpms.observability import (
    CompositeTraceSink,
    InMemoryTraceSink,
    SafeJsonlTraceSink,
)
from agentpms.runtime import build_orchestrator, build_pms_adapter
from agentpms.safety import DecisionGroundingPolicy

from .app import create_app


settings = Settings.from_env()
pms = build_pms_adapter(settings)
memory_trace = InMemoryTraceSink()
trace_sink = CompositeTraceSink(
    memory_trace,
    SafeJsonlTraceSink(Path("logs/agentpms.jsonl")),
)

if settings.decision_provider == "deepseek":
    if not settings.llm_api_key or settings.llm_api_key == "EMPTY":
        raise ValueError(
            "DEEPSEEK_API_KEY doit etre configuree localement dans .env"
        )
    decision_model = DeepSeekDecisionModel(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
    )
elif settings.decision_provider == "openai_compatible":
    decision_model = OpenAICompatibleDecisionModel(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
elif settings.decision_provider == "local_rules":
    decision_model = LocalRulesDecisionModel()
else:
    raise ValueError(
        "AGENTPMS_DECISION_PROVIDER doit valoir deepseek, local_rules "
        "ou openai_compatible"
    )

orchestrator = build_orchestrator(
    settings=settings,
    decision_model=decision_model,
    pms=pms,
    knowledge=(
        HTTPKnowledgeClient(settings.knowledge_url)
        if settings.knowledge_enabled
        else None
    ),
    decision_grounding=(
        DecisionGroundingPolicy(
            default_room_selection_strategy=(
                settings.default_room_selection_strategy
            )
        )
        if settings.decision_provider in {"deepseek", "openai_compatible"}
        else None
    ),
    trace_sink=trace_sink,
)

app = create_app(
    orchestrator=orchestrator,
    pms=pms,
    trace_sink=memory_trace,
    decision_mode=settings.decision_provider,
)
