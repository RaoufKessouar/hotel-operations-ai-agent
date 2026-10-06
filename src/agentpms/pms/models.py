"""Modeles metier independants du fournisseur de PMS."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class PMSStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    FORBIDDEN = "forbidden"
    ERROR = "error"


class ExtraLine(BaseModel):
    code: str
    label: str
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(ge=0)


class Reservation(BaseModel):
    reservation_id: str
    guest_name: str
    arrival_date: date
    departure_date: date
    room_id: str | None = None
    room_type: str | None = None
    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    status: str = "confirmed"
    extras: list[ExtraLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_stay(self) -> "Reservation":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date doit etre posterieure a arrival_date")
        return self


class AvailabilityOption(BaseModel):
    room_id: str
    room_type: str
    available: bool


class PMSResult(BaseModel):
    status: PMSStatus
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    external_operation_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    verified: bool = False
