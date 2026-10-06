"""Confirmations courtes, exactes et consommables une seule fois."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from agentpms.contracts import ConfirmationPrompt, ToolCall


def _fingerprint(call: ToolCall) -> str:
    payload = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _ConfirmationRecord:
    request_id: str
    actor_id: str
    fingerprint: str
    expires_at: datetime
    used: bool = False


class ConfirmationManager:
    def __init__(self, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds doit etre positif")
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, _ConfirmationRecord] = {}

    def issue(
        self,
        *,
        request_id: str,
        actor_id: str,
        call: ToolCall,
        summary: str,
    ) -> ConfirmationPrompt:
        confirmation_id = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.ttl_seconds
        )
        self._records[confirmation_id] = _ConfirmationRecord(
            request_id=request_id,
            actor_id=actor_id,
            fingerprint=_fingerprint(call),
            expires_at=expires_at,
        )
        return ConfirmationPrompt(
            confirmation_id=confirmation_id,
            request_id=request_id,
            tool_name=call.name,
            summary=summary,
            expires_at=expires_at,
        )

    def consume(
        self,
        confirmation_id: str,
        *,
        request_id: str,
        actor_id: str,
        call: ToolCall,
    ) -> tuple[bool, str]:
        record = self._records.get(confirmation_id)
        if record is None:
            return False, "Confirmation inconnue."
        if record.used:
            return False, "Confirmation deja utilisee."
        if datetime.now(timezone.utc) >= record.expires_at:
            return False, "Confirmation expiree."
        if record.request_id != request_id or record.actor_id != actor_id:
            return False, "Confirmation non associee a cet utilisateur ou cette requete."
        if record.fingerprint != _fingerprint(call):
            return False, "Les parametres ont change : une nouvelle confirmation est requise."
        record.used = True
        return True, "Confirmation valide."

    def revoke(self, confirmation_id: str) -> None:
        record = self._records.get(confirmation_id)
        if record is not None:
            record.used = True

