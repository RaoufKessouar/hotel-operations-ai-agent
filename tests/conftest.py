from __future__ import annotations

from datetime import date

import pytest

from agentpms.pms.in_memory import InMemoryPMSAdapter
from agentpms.pms.models import AvailabilityOption, Reservation


@pytest.fixture
def pms() -> InMemoryPMSAdapter:
    return InMemoryPMSAdapter(
        reservations=[
            Reservation(
                reservation_id="R-4587",
                guest_name="Jean Dupont",
                arrival_date=date(2026, 9, 12),
                departure_date=date(2026, 9, 14),
                room_id="204",
                room_type="double",
            ),
            Reservation(
                reservation_id="R-4588",
                guest_name="Marie Dupont",
                arrival_date=date(2026, 9, 15),
                departure_date=date(2026, 9, 17),
                room_id="310",
                room_type="double",
            ),
        ],
        rooms=[
            AvailabilityOption(room_id="204", room_type="double", available=False),
            AvailabilityOption(room_id="310", room_type="double", available=True),
        ],
        customer_requests=[
            {
                "request_id": "12",
                "subject": "Arrivee tardive",
                "customer_name": "Jean Dupont",
                "reservation_id": "R-4587",
                "status": "open",
                "created_at": "2026-09-11 10:00:00",
                "updated_at": "2026-09-11 10:00:00",
                "messages": [
                    {
                        "message_id": "M-1",
                        "sender": "customer",
                        "message": "Puis-je arriver apres 22 h ?",
                        "created_at": "2026-09-11 10:00:00",
                    }
                ],
            }
        ],
    )
