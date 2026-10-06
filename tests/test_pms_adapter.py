from datetime import date
from decimal import Decimal

from agentpms.pms.models import PMSStatus


def test_list_arrivals_filters_the_requested_day(pms):
    result = pms.list_arrivals(arrival_date=date(2026, 9, 12))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["arrival_date"] == "2026-09-12"
    assert [item["guest_name"] for item in result.data["reservations"]] == [
        "Jean Dupont"
    ]


def test_list_departures_filters_the_requested_day(pms):
    result = pms.list_departures(departure_date=date(2026, 9, 14))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["departure_date"] == "2026-09-14"
    assert [item["guest_name"] for item in result.data["reservations"]] == [
        "Jean Dupont"
    ]


def test_query_stays_uses_hotel_presence_interval(pms):
    result = pms.query_stays(stay_date=date(2026, 9, 13))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["stay_date"] == "2026-09-13"
    assert [item["guest_name"] for item in result.data["reservations"]] == [
        "Jean Dupont"
    ]


def test_query_stays_supports_combined_filters_and_excludes_departure_night(pms):
    result = pms.query_stays(
        stay_date=date(2026, 9, 14),
        guest_name="Dupont",
        room_type="double",
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["reservations"] == []


def test_occupancy_counts_rooms_used_for_the_requested_night(pms):
    result = pms.get_occupancy(stay_date=date(2026, 9, 12))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data == {
        "stay_date": "2026-09-12",
        "total_rooms": 2,
        "occupied_rooms": 1,
        "available_rooms": 1,
        "occupancy_rate_percent": 50.0,
    }


def test_occupancy_range_returns_each_night_and_a_weighted_average(pms):
    result = pms.get_occupancy_range(
        start_date=date(2026, 9, 12),
        end_date=date(2026, 9, 15),
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["average_occupancy_rate_percent"] == 37.5
    assert [day["occupied_rooms"] for day in result.data["days"]] == [1, 1, 0, 1]


def test_search_reports_ambiguity_instead_of_guessing(pms):
    result = pms.search_reservations(guest_name="Dupont")

    assert result.status == PMSStatus.AMBIGUOUS
    assert len(result.data["reservations"]) == 2


def test_create_reservation_is_verified_and_idempotent(pms):
    arguments = {
        "guest_first_name": "Pierre",
        "guest_last_name": "Delafitte",
        "email": "pierre.delafitte@agentpms.test",
        "phone": "0600000000",
        "arrival_date": date(2026, 9, 2),
        "departure_date": date(2026, 9, 10),
        "adults": 2,
        "children": 0,
        "room_type_id": "double",
        "room_id": "310",
        "idempotency_key": "request-create:call-create",
    }

    first = pms.create_reservation(**arguments)
    second = pms.create_reservation(**arguments)

    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert second.data == first.data
    assert first.data["reservation"]["guest_name"] == "Pierre Delafitte"


def test_update_reservation_is_verified_and_idempotent(pms):
    arguments = {
        "reservation_id": "R-4587",
        "arrival_date": None,
        "departure_date": date(2026, 9, 15),
        "adults": 2,
        "children": 1,
        "idempotency_key": "request-update:0:update_reservation",
    }

    first = pms.update_reservation(**arguments)
    second = pms.update_reservation(**arguments)

    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert first.data["reservation"]["departure_date"] == "2026-09-15"
    assert first.data["reservation"]["adults"] == 2
    assert first.data["reservation"]["children"] == 1
    assert second.data == first.data


def test_check_in_then_check_out_are_verified_and_idempotent(pms):
    checked_in = pms.check_in_reservation(
        reservation_id="R-4587",
        idempotency_key="request:0:check_in_reservation",
    )
    checked_in_retry = pms.check_in_reservation(
        reservation_id="R-4587",
        idempotency_key="request:0:check_in_reservation",
    )
    checked_out = pms.check_out_reservation(
        reservation_id="R-4587",
        idempotency_key="request:1:check_out_reservation",
    )

    assert checked_in.status == PMSStatus.SUCCESS
    assert checked_in.verified is True
    assert checked_in.data["reservation"]["status"] == "checked_in"
    assert checked_in_retry.data == checked_in.data
    assert checked_out.status == PMSStatus.SUCCESS
    assert checked_out.verified is True
    assert checked_out.data["reservation"]["status"] == "checked_out"


def test_issue_invoice_is_confirmable_verified_and_idempotent(pms):
    first = pms.issue_invoice(
        reservation_id="R-4587",
        idempotency_key="request:0:issue_invoice",
    )
    second = pms.issue_invoice(
        reservation_id="R-4587",
        idempotency_key="request:0:issue_invoice",
    )

    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert first.data["invoice"]["reservation_id"] == "R-4587"
    assert second.data == first.data
    assert pms.get_invoice(reservation_id="R-4587").data == first.data


def test_customer_request_reply_is_verified_and_idempotent(pms):
    listed = pms.list_customer_requests(status="open")
    first = pms.reply_customer_request(
        request_id="12",
        message="Oui, nous vous attendrons.",
        idempotency_key="request:0:reply_customer_request",
    )
    second = pms.reply_customer_request(
        request_id="12",
        message="Oui, nous vous attendrons.",
        idempotency_key="request:0:reply_customer_request",
    )

    assert listed.data["requests"][0]["request_id"] == "12"
    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert first.data["request"]["messages"][-1]["sender"] == "hotel"
    assert second.data == first.data


def test_add_extra_is_verified_and_idempotent(pms):
    arguments = {
        "reservation_id": "R-4587",
        "extra_code": "BREAKFAST",
        "label": "Petit-dejeuner",
        "quantity": 1,
        "unit_price": Decimal("15.00"),
        "idempotency_key": "request-1:call-1",
    }

    first = pms.add_extra(**arguments)
    second = pms.add_extra(**arguments)
    reservation = pms.get_reservation("R-4587")

    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert second.external_operation_id == first.external_operation_id
    assert len(reservation.data["reservation"]["extras"]) == 1


def test_missing_reservation_never_reports_success(pms):
    result = pms.add_extra(
        reservation_id="UNKNOWN",
        extra_code="BREAKFAST",
        label="Petit-dejeuner",
        quantity=1,
        unit_price=Decimal("15.00"),
        idempotency_key="request-2:call-1",
    )

    assert result.status == PMSStatus.NOT_FOUND
    assert result.verified is False
