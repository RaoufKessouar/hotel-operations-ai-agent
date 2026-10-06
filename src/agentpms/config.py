"""Configuration par variables d'environnement, sans secret dans le depot."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit etre un entier") from exc
    if value <= 0:
        raise ValueError(f"{name} doit etre strictement positif")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit etre un nombre") from exc
    if value <= 0:
        raise ValueError(f"{name} doit etre strictement positif")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} doit etre un booleen")


def _room_selection_strategy(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().casefold()
    if value not in {"cheapest", "most_expensive", "ask"}:
        raise ValueError(
            f"{name} doit valoir cheapest, most_expensive ou ask"
        )
    return value


@dataclass(frozen=True)
class Settings:
    rag_project: Path | None
    knowledge_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    max_steps: int
    confirmation_ttl_seconds: int
    llm_timeout_seconds: float = 240
    decision_provider: str = "openai_compatible"
    knowledge_enabled: bool = False
    pms_provider: str = "qloapps"
    qloapps_base_url: str = "http://127.0.0.1:8088/api"
    qloapps_api_key: str = ""
    qloapps_write_api_key: str = ""
    qloapps_hotel_id: int = 1
    qloapps_timeout_seconds: float = 15
    qloapps_booking_scan_limit: int = 200
    qloapps_employee_id: int = 1
    llm_reasoning_effort: str = "low"
    default_room_selection_strategy: str = "ask"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        rag_value = os.getenv("AGENTPMS_RAG_PROJECT", "").strip()
        return cls(
            rag_project=Path(rag_value).expanduser() if rag_value else None,
            knowledge_url=os.getenv(
                "AGENTPMS_KNOWLEDGE_URL",
                "http://127.0.0.1:8765/knowledge/query",
            ),
            llm_base_url=os.getenv(
                "AGENTPMS_LLM_BASE_URL", "https://api.deepseek.com/beta"
            ),
            llm_api_key=os.getenv(
                "DEEPSEEK_API_KEY",
                os.getenv("AGENTPMS_LLM_API_KEY", ""),
            ),
            llm_model=os.getenv(
                "AGENTPMS_LLM_MODEL", "deepseek-v4-flash"
            ),
            max_steps=_positive_int("AGENTPMS_MAX_STEPS", 6),
            confirmation_ttl_seconds=_positive_int(
                "AGENTPMS_CONFIRMATION_TTL_SECONDS", 300
            ),
            llm_timeout_seconds=_positive_float(
                "AGENTPMS_LLM_TIMEOUT_SECONDS", 240
            ),
            decision_provider=os.getenv(
                "AGENTPMS_DECISION_PROVIDER", "deepseek"
            ).strip(),
            knowledge_enabled=_boolean("AGENTPMS_KNOWLEDGE_ENABLED", False),
            pms_provider=os.getenv("AGENTPMS_PMS_PROVIDER", "qloapps").strip(),
            qloapps_base_url=os.getenv(
                "AGENTPMS_QLOAPPS_BASE_URL", "http://127.0.0.1:8088/api"
            ).strip(),
            qloapps_api_key=os.getenv("AGENTPMS_QLOAPPS_API_KEY", "").strip(),
            qloapps_write_api_key=os.getenv(
                "AGENTPMS_QLOAPPS_WRITE_API_KEY", ""
            ).strip(),
            qloapps_hotel_id=_positive_int("AGENTPMS_QLOAPPS_HOTEL_ID", 1),
            qloapps_timeout_seconds=_positive_float(
                "AGENTPMS_QLOAPPS_TIMEOUT_SECONDS", 15
            ),
            qloapps_booking_scan_limit=_positive_int(
                "AGENTPMS_QLOAPPS_BOOKING_SCAN_LIMIT", 200
            ),
            qloapps_employee_id=_positive_int("AGENTPMS_QLOAPPS_EMPLOYEE_ID", 1),
            llm_reasoning_effort=os.getenv(
                "AGENTPMS_LLM_REASONING_EFFORT", "low"
            ).strip(),
            default_room_selection_strategy=_room_selection_strategy(
                "AGENTPMS_DEFAULT_ROOM_SELECTION", "ask"
            ),
        )
