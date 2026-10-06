"""Surface HTTP locale. Les cles PMS ne quittent jamais le serveur Python."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agentpms.agent import AgentOrchestrator
from agentpms.contracts import AgentResponse
from agentpms.observability import InMemoryTraceSink
from agentpms.pms import PMSAdapter


class StartRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)
    actor_id: str = Field(min_length=3, max_length=100)


class ContinueRequest(StartRequest):
    pass


class ConfirmationRequest(BaseModel):
    confirmation_id: str = Field(min_length=10, max_length=200)
    approved: bool
    actor_id: str = Field(min_length=3, max_length=100)


def create_app(
    *,
    orchestrator: AgentOrchestrator,
    pms: PMSAdapter,
    trace_sink: InMemoryTraceSink,
    decision_mode: str,
) -> FastAPI:
    app = FastAPI(
        title="AgentPMS",
        description="Assistant local de reception hoteliere",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    static_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ready",
            "pms": pms.name,
            "capabilities": sorted(pms.capabilities()),
            "decision_mode": decision_mode,
            "grounding_enabled": orchestrator.decision_grounding is not None,
        }

    @app.post("/api/requests", response_model=AgentResponse)
    def start(payload: StartRequest) -> AgentResponse:
        return orchestrator.start(
            payload.message,
            actor_id=payload.actor_id,
            role="reception",
        )

    @app.post(
        "/api/requests/{request_id}/messages",
        response_model=AgentResponse,
    )
    def continue_request(request_id: str, payload: ContinueRequest) -> AgentResponse:
        return orchestrator.continue_request(
            request_id=request_id,
            user_message=payload.message,
            actor_id=payload.actor_id,
        )

    @app.post(
        "/api/requests/{request_id}/confirmation",
        response_model=AgentResponse,
    )
    def confirm(request_id: str, payload: ConfirmationRequest) -> AgentResponse:
        return orchestrator.confirm(
            request_id=request_id,
            confirmation_id=payload.confirmation_id,
            approved=payload.approved,
            actor_id=payload.actor_id,
        )

    @app.get("/api/requests/{request_id}/trace")
    def trace(request_id: str) -> dict[str, object]:
        events = [
            event.safe_payload()
            for event in trace_sink.events
            if event.request_id == request_id
        ]
        return {"request_id": request_id, "events": events}

    return app
