from datetime import date

from agentpms.contracts import ToolStatus
from agentpms.pms import PMSResult, PMSStatus
from agentpms.tools import ToolContext
from agentpms.tools.pms_tools import (
    CreateReservationsInput,
    CreateReservationsTool,
    GetReservationsInput,
    GetReservationsTool,
    IssueInvoicesInput,
    IssueInvoicesTool,
    PlanRoomAllocationInput,
    PlanRoomAllocationTool,
)


class CompositePMS:
    def __init__(self) -> None:
        self.create_calls = []
        self.invoice_calls = []

    def check_availability(self, **arguments):
        rooms = [
            {
                "room_id": f"D{index}",
                "room_number": f"DR-{100 + index}",
                "room_type": "Delux Rooms",
                "room_type_id": "2",
                "max_adults": 2,
                "max_children": 1,
                "available": True,
            }
            for index in range(1, 5)
        ] + [
            {
                "room_id": f"E{index}",
                "room_number": f"ER-{100 + index}",
                "room_type": "Executive Rooms",
                "room_type_id": "3",
                "max_adults": 2,
                "max_children": 1,
                "available": True,
            }
            for index in range(1, 4)
        ]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Disponibilites verifiees.",
            data={
                "arrival_date": arguments["arrival_date"].isoformat(),
                "departure_date": arguments["departure_date"].isoformat(),
                "adults": arguments["adults"],
                "children": arguments["children"],
                "currency": "EUR",
                "rooms": rooms,
                "room_types": [
                    {
                        "room_type_id": "2",
                        "name": "Delux Rooms",
                        "available_count": 4,
                    },
                    {
                        "room_type_id": "3",
                        "name": "Executive Rooms",
                        "available_count": 3,
                    },
                ],
            },
            verified=True,
        )

    def quote_stay(self, **arguments):
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Devis verifies.",
            data={
                "currency": "EUR",
                "tourist_tax": {
                    "status": "not_configured",
                    "included_in_totals": False,
                },
                "room_quotes": [
                    {
                        "room_type_id": "2",
                        "room_stay_with_tax": "2100.00",
                        "breakfast_stay_with_tax": "0.00",
                        "total_stay_excluding_tourist_tax": "2100.00",
                    },
                    {
                        "room_type_id": "3",
                        "room_stay_with_tax": "2700.00",
                        "breakfast_stay_with_tax": "0.00",
                        "total_stay_excluding_tourist_tax": "2700.00",
                    },
                ],
            },
            verified=True,
        )

    def preview_create_reservation(self, **arguments):
        return f"Creer {arguments['room_id']}. Confirmer ?"

    def create_reservation(self, **arguments):
        self.create_calls.append(arguments)
        number = len(self.create_calls)
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation creee.",
            data={
                "reservation": {
                    "reservation_id": str(100 + number),
                    "guest_name": (
                        f"{arguments['guest_first_name']} "
                        f"{arguments['guest_last_name']}"
                    ),
                    "room_id": arguments["room_id"],
                    "room_type": arguments["room_type_id"],
                    "arrival_date": arguments["arrival_date"].isoformat(),
                    "departure_date": arguments["departure_date"].isoformat(),
                }
            },
            external_operation_id=arguments["idempotency_key"],
            verified=True,
        )

    def issue_invoice(self, **arguments):
        self.invoice_calls.append(arguments)
        reservation_id = arguments["reservation_id"]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Facture emise.",
            data={
                "invoice": {
                    "number": f"INV-{reservation_id}",
                    "reservation_id": reservation_id,
                    "total_including_tax": "2100.00",
                    "currency": "EUR",
                }
            },
            external_operation_id=arguments["idempotency_key"],
            verified=True,
        )

    def get_reservation(self, reservation_id):
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation trouvee.",
            data={
                "reservation": {
                    "reservation_id": str(reservation_id),
                    "guest_name": "Client Test",
                }
            },
            verified=True,
        )


def _context() -> ToolContext:
    return ToolContext(
        request_id="request-1",
        call_id="call-1",
        actor_id="reception",
        role="reception",
        idempotency_key="batch-key",
    )


def test_room_allocation_selects_the_six_cheapest_distinct_rooms():
    pms = CompositePMS()
    tool = PlanRoomAllocationTool(pms)
    arguments = PlanRoomAllocationInput(
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 11),
        requirements=[
            {"reference": f"EGC-{index}", "adults": 1, "children": 0}
            for index in range(1, 7)
        ],
        selection_strategy="cheapest",
    )

    result = tool.execute(arguments, _context())

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert result.data["requested_count"] == 6
    assert result.data["total_stay_excluding_tourist_tax"] == "13800.00"
    allocations = result.data["allocations"]
    assert len({item["room_id"] for item in allocations}) == 6
    assert [item["room_type_id"] for item in allocations].count("2") == 4
    assert [item["room_type_id"] for item in allocations].count("3") == 2
    assert "Total des 6 chambres : 13800.00 EUR TTC" in result.message


def test_room_allocation_rejects_rooms_below_the_requested_capacity():
    tool = PlanRoomAllocationTool(CompositePMS())

    result = tool.execute(
        PlanRoomAllocationInput(
            arrival_date=date(2026, 9, 10),
            departure_date=date(2026, 9, 11),
            requirements=[
                {"reference": "family-1", "adults": 3, "children": 0}
            ],
            selection_strategy="cheapest",
        ),
        _context(),
    )

    assert result.status == ToolStatus.CONFLICT
    assert result.error_code == "insufficient_room_allocation"


def test_create_reservations_executes_distinct_verified_operations():
    pms = CompositePMS()
    tool = CreateReservationsTool(pms)
    arguments = CreateReservationsInput(
        reservations=[
            {
                "reference": "EGC-1",
                "guest_first_name": "EGC",
                "guest_last_name": "group",
                "email": None,
                "phone": None,
                "arrival_date": "2026-09-10",
                "departure_date": "2026-09-11",
                "adults": 1,
                "children": 0,
                "room_type_id": "2",
                "room_id": "D1",
            },
            {
                "reference": "EGC-2",
                "guest_first_name": "EGC",
                "guest_last_name": "group",
                "email": None,
                "phone": None,
                "arrival_date": "2026-09-10",
                "departure_date": "2026-09-11",
                "adults": 1,
                "children": 0,
                "room_type_id": "3",
                "room_id": "E1",
            },
        ]
    )

    result = tool.execute(arguments, _context())

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert result.data["created_count"] == 2
    assert [item["reservation_id"] for item in result.data["reservations"]] == [
        "101",
        "102",
    ]
    keys = [call["idempotency_key"] for call in pms.create_calls]
    assert len(keys) == len(set(keys)) == 2


def test_issue_invoices_executes_and_reports_every_reservation():
    pms = CompositePMS()
    tool = IssueInvoicesTool(pms)

    result = tool.execute(
        IssueInvoicesInput(reservation_ids=["101", "102"]),
        _context(),
    )

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert [item["number"] for item in result.data["invoices"]] == [
        "INV-101",
        "INV-102",
    ]
    assert len({call["idempotency_key"] for call in pms.invoice_calls}) == 2


def test_get_reservations_verifies_every_requested_identifier():
    tool = GetReservationsTool(CompositePMS())

    result = tool.execute(
        GetReservationsInput(reservation_ids=["101", "102"]),
        _context(),
    )

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert [item["reservation_id"] for item in result.data["reservations"]] == [
        "101",
        "102",
    ]
