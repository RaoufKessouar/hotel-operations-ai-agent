"""Traces du workflow sans exposer les raisonnements internes du modele."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


_SENSITIVE_KEYS = {
    "guest",
    "guest_name",
    "first_name",
    "last_name",
    "full_name",
    "email",
    "phone",
    "address",
    "card",
    "password",
    "token",
    "api_key",
}


def _redact(value: Any, key: str = "") -> Any:
    folded_key = key.casefold()
    if folded_key in _SENSITIVE_KEYS or any(
        folded_key.endswith(f"_{part}") for part in _SENSITIVE_KEYS
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TraceEvent(BaseModel):
    event_type: str
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tool_name: str | None = None
    status: str | None = None
    duration_ms: float | None = None
    fields: dict[str, Any] = Field(default_factory=dict)

    def safe_payload(self) -> dict[str, Any]:
        return _redact(self.model_dump(mode="json"))


class TraceSink(Protocol):
    def record(self, event: TraceEvent) -> None: ...


class NullTraceSink:
    def record(self, event: TraceEvent) -> None:
        return None


class InMemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(self, event: TraceEvent) -> None:
        self.events.append(event.model_copy(deep=True))


class CompositeTraceSink:
    def __init__(self, *sinks: TraceSink) -> None:
        self.sinks = sinks

    def record(self, event: TraceEvent) -> None:
        for sink in self.sinks:
            sink.record(event)


class SafeJsonlTraceSink:
    def __init__(self, destination: Path) -> None:
        self.destination = destination

    def record(self, event: TraceEvent) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        with self.destination.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event.safe_payload(), ensure_ascii=False) + "\n")
