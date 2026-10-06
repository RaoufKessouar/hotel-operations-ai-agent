from copy import deepcopy
from datetime import date
from decimal import Decimal

from agentpms.pms.models import PMSStatus
from agentpms.pms.qloapps import QloAppsAPIError, QloAppsAdapter


def _booking(identifier, firstname="Alice", lastname="Dupont"):
    return {
        "id": identifier,
        "id_property": 1,
        "currency": "EUR",
        "booking_status": 1,
        "associations": {
            "customer_detail": {"firstname": firstname, "lastname": lastname},
            "room_types": [
                {
                    "id_room_type": 2,
                    "name": "Delux Rooms",
                    "checkin_date": "2026-09-10",
                    "checkout_date": "2026-09-12",
                    "rooms": [
                        {
                            "id_room": 7,
                            "id_hotel_booking": identifier,
                            "adults": 2,
                            "child": 0,
                            "facilities": [],
                            "services": [
                                {
                                    "id_service": 6,
                                    "name": "Breakfast",
                                    "quantity": 2,
                                    "unit_price_without_tax": "15.00",
                                }
                            ],
                        }
                    ],
                }
            ],
            "cart_rules": [],
        },
    }


class FakeClient:
    def __init__(self):
        self.bookings = {
            "42": _booking(42),
            "43": _booking(43, firstname="Alix"),
        }
        self.availability = {
            "currency": "EUR",
            "room_types": [
                {
                    "id_room_type": 2,
                    "name": "Delux Rooms",
                    "base_price": "175.00",
                    "total_price": "350.00",
                    "base_price_with_tax": "210.00",
                    "total_price_with_tax": "420.00",
                    "rooms": {
                        "available": {
                            "7": {"id_room": 7, "room_num": "DR-102"}
                        }
                    },
                },
                {
                    "id_room_type": 4,
                    "name": "Luxury Rooms",
                    "base_price": "275.00",
                    "total_price": "550.00",
                    "base_price_with_tax": "330.00",
                    "total_price_with_tax": "660.00",
                    "rooms": {
                        "available": {
                            "17": {"id_room": 17, "room_num": "LR-102"}
                        }
                    },
                },
            ]
        }
        self.hotel_rooms_calls = 0
        self.room_bookings_calls = 0
        self.room_bookings = {
            "42": {
                "id": 42,
                "id_hotel": 1,
                "id_room": 7,
                "id_order": 42,
                "date_from": "2026-09-10 00:00:00",
                "date_to": "2026-09-12 00:00:00",
                "id_status": 1,
                "check_in": "0000-00-00 00:00:00",
                "check_out": "0000-00-00 00:00:00",
                "is_cancelled": 0,
            },
            "2": {
                "id": 2,
                "id_hotel": 1,
                "id_room": 8,
                "date_from": "2026-09-10 00:00:00",
                "date_to": "2026-09-12 00:00:00",
                "id_status": 1,
                "check_in": "0000-00-00 00:00:00",
                "check_out": "0000-00-00 00:00:00",
                "is_cancelled": 1,
            },
        }
        self.orders = {
            "42": {
                "id": 42,
                "reference": "AGENT42",
                "currency": "EUR",
                "total_paid_tax_excl": "350.00",
                "total_paid_tax_incl": "420.00",
            }
        }
        self.invoices = {}
        self.customer_threads = {
            "12": {
                "id": 12,
                "id_order": 42,
                "user_name": "Alice Dupont",
                "email": "alice@example.test",
                "subject": "Arrivee tardive",
                "status": "1",
                "date_add": "2026-09-09 10:00:00",
                "date_upd": "2026-09-09 10:00:00",
                "associations": {"customer_messages": [{"id": 21}]},
            }
        }
        self.customer_messages = {
            "21": {
                "id": 21,
                "id_customer_thread": 12,
                "id_employee": 0,
                "message": "Puis-je arriver apres 22 h ?",
                "date_add": "2026-09-09 10:00:00",
            }
        }

    def list_booking_ids(self):
        return list(self.bookings)

    def get_booking(self, reservation_id):
        if reservation_id not in self.bookings:
            raise QloAppsAPIError("Reservation absente", status_code=404)
        return self.bookings[reservation_id]

    def query_availability(self, **kwargs):
        availability = deepcopy(self.availability)
        arrival = kwargs.get("arrival_date")
        departure = kwargs.get("departure_date")
        if arrival is not None and departure is not None:
            nights = (departure - arrival).days
            for category in availability["room_types"]:
                category["total_price"] = str(
                    Decimal(category["base_price"]) * nights
                )
                category["total_price_with_tax"] = str(
                    Decimal(category["base_price_with_tax"]) * nights
                )
        return availability

    def list_hotel_rooms(self, hotel_id):
        assert hotel_id == 1
        self.hotel_rooms_calls += 1
        return [
            {"id": 7, "id_hotel": 1, "room_num": "DR-102"},
            {"id": 8, "id_hotel": 1, "room_num": "DR-103"},
        ]

    def list_room_bookings(self, hotel_id):
        assert hotel_id == 1
        self.room_bookings_calls += 1
        return [deepcopy(item) for item in self.room_bookings.values()]

    def get_room_booking(self, room_booking_id):
        try:
            return deepcopy(self.room_bookings[str(room_booking_id)])
        except KeyError as exc:
            raise QloAppsAPIError("Sejour absent", status_code=404) from exc

    def get_order(self, order_id):
        try:
            return deepcopy(self.orders[str(order_id)])
        except KeyError as exc:
            raise QloAppsAPIError("Commande absente", status_code=404) from exc

    def list_order_invoices(self, order_id):
        return [
            deepcopy(item)
            for item in self.invoices.values()
            if str(item["id_order"]) == str(order_id)
        ]

    def list_order_details(self, order_id):
        if str(order_id) != "42":
            return []
        return [
            {
                "id": 8,
                "id_order": 42,
                "id_order_invoice": 1,
                "product_name": "Delux Rooms",
                "product_quantity": 1,
                "unit_price_tax_excl": "350.00",
                "unit_price_tax_incl": "420.00",
                "total_price_tax_excl": "350.00",
                "total_price_tax_incl": "420.00",
            }
        ]

    def list_customer_threads(self):
        return [deepcopy(item) for item in self.customer_threads.values()]

    def get_customer_thread(self, thread_id):
        try:
            return deepcopy(self.customer_threads[str(thread_id)])
        except KeyError as exc:
            raise QloAppsAPIError("Demande absente", status_code=404) from exc

    def get_customer_message(self, message_id):
        try:
            return deepcopy(self.customer_messages[str(message_id)])
        except KeyError as exc:
            raise QloAppsAPIError("Message absent", status_code=404) from exc

    def list_customer_messages(self, thread_id):
        return [
            deepcopy(item)
            for item in self.customer_messages.values()
            if str(item["id_customer_thread"]) == str(thread_id)
        ]

    def list_services(self):
        return [
            {
                "id": 9,
                "name": [{"id": 1, "value": "Breakfast"}],
                "price": "350.00",
                "price_calculation_method": 2,
                "id_tax_rules_group": 1,
            }
        ]

    def get_uniform_tax_rate(self, tax_rules_group_id):
        assert tax_rules_group_id == 1
        return Decimal("20.00")

    def get_room_type(self, room_type_id):
        return {"id": int(room_type_id), "id_tax_rules_group": 1}


class FakeWriteClient:
    def __init__(
        self,
        read_client,
        *,
        fail_after_write=False,
        applied_tax_rate=Decimal("0.20"),
    ):
        self.read_client = read_client
        self.fail_after_write = fail_after_write
        self.applied_tax_rate = applied_tax_rate
        self.calls = []
        self.create_calls = []
        self.room_status_calls = []
        self.invoice_calls = []
        self.customer_message_calls = []

    def create_booking(self, **kwargs):
        self.create_calls.append(deepcopy(kwargs))
        identifier = str(max(int(value) for value in self.read_client.bookings) + 1)
        booking = _booking(
            int(identifier),
            firstname=kwargs["guest_first_name"],
            lastname=kwargs["guest_last_name"],
        )
        booking["source"] = kwargs["operation_marker"]
        customer = booking["associations"]["customer_detail"]
        customer["email"] = kwargs["email"]
        customer["phone"] = kwargs["phone"]
        room_type = booking["associations"]["room_types"][0]
        room_type["id_room_type"] = kwargs["room_type_id"]
        room_type["checkin_date"] = kwargs["arrival_date"].isoformat()
        room_type["checkout_date"] = kwargs["departure_date"].isoformat()
        room = room_type["rooms"][0]
        room["id_room"] = kwargs["room_id"]
        room["adults"] = kwargs["adults"]
        room["child"] = kwargs["children"]
        nights = (kwargs["departure_date"] - kwargs["arrival_date"]).days
        qloapps_divisor = max(nights - 1, 1)
        room_total_without_tax = (
            kwargs["room_unit_price_without_tax"] / qloapps_divisor * nights
        )
        room["unit_price_without_tax"] = str(room_total_without_tax)
        room["total_tax"] = str(room_total_without_tax * self.applied_tax_rate)
        room["services"] = []
        self.read_client.bookings[identifier] = booking
        if self.fail_after_write:
            raise QloAppsAPIError(
                "Erreur QloApps apres enregistrement",
                status_code=500,
                retryable=True,
            )
        return {"id": int(identifier)}

    def update_booking_services(self, booking):
        self.calls.append(deepcopy(booking))
        stored = deepcopy(booking)
        for room_type in stored["associations"]["room_types"]:
            nights = (
                date.fromisoformat(room_type["checkout_date"])
                - date.fromisoformat(room_type["checkin_date"])
            ).days
            for room in room_type["rooms"]:
                for service in room.get("services", []):
                    service.pop("_agentpms_new", None)
                    if str(service.get("id_service")) == "9":
                        service["name"] = "Breakfast"
                        subtotal = Decimal("350.00") * service["quantity"] * nights
                        service["unit_price_without_tax"] = str(
                            subtotal / service["quantity"]
                        )
                        service["total_price_without_tax"] = str(subtotal)
                        service["total_tax"] = str(subtotal * self.applied_tax_rate)
                        service["per_night"] = 1
                        service["price_mode"] = 2
        self.read_client.bookings[str(stored["id"])] = stored
        if self.fail_after_write:
            raise QloAppsAPIError(
                "Erreur QloApps apres enregistrement",
                status_code=500,
                retryable=True,
            )
        return stored

    def update_booking(self, booking):
        return self.update_booking_services(booking)

    def update_booking_via_backoffice(
        self,
        *,
        reservation_id,
        arrival_date,
        departure_date,
        adults,
        children,
        idempotency_key,
    ):
        booking = deepcopy(self.read_client.bookings[str(reservation_id)])
        room_type = booking["associations"]["room_types"][0]
        room_type["checkin_date"] = arrival_date.isoformat()
        room_type["checkout_date"] = departure_date.isoformat()
        room = room_type["rooms"][0]
        room["adults"] = adults
        room["child"] = children
        return self.update_booking_services(booking)

    def update_room_booking_status(self, booking, *, status, occurred_at):
        self.room_status_calls.append(
            {
                "booking": deepcopy(booking),
                "status": status,
                "occurred_at": occurred_at,
            }
        )
        stored = deepcopy(booking)
        stored["id_status"] = status
        timestamp = occurred_at.replace(microsecond=0).isoformat(sep=" ")
        stored["check_in" if status == 2 else "check_out"] = timestamp
        self.read_client.room_bookings[str(stored["id"])] = stored
        return deepcopy(stored)

    def issue_invoice(self, *, order_id, idempotency_key):
        self.invoice_calls.append(
            {"order_id": str(order_id), "idempotency_key": idempotency_key}
        )
        invoice = {
            "id": 1,
            "id_order": int(order_id),
            "number": 1001,
            "total_paid_tax_excl": "350.00",
            "total_paid_tax_incl": "420.00",
            "date_add": "2026-09-12 11:00:00",
        }
        self.read_client.invoices["1"] = invoice
        return [deepcopy(invoice)]

    def create_customer_message(self, *, thread_id, employee_id, message):
        self.customer_message_calls.append(
            {
                "thread_id": str(thread_id),
                "employee_id": employee_id,
                "message": message,
            }
        )
        identifier = str(max(int(value) for value in self.read_client.customer_messages) + 1)
        created = {
            "id": int(identifier),
            "id_customer_thread": int(thread_id),
            "id_employee": employee_id,
            "message": message,
            "date_add": "2026-09-09 11:00:00",
        }
        self.read_client.customer_messages[identifier] = created
        self.read_client.customer_threads[str(thread_id)]["associations"][
            "customer_messages"
        ].append({"id": int(identifier)})
        return deepcopy(created)


def _remove_existing_services(client):
    room = client.bookings["42"]["associations"]["room_types"][0]["rooms"][0]
    room["services"] = []


def test_adapter_maps_a_realistic_qloapps_booking():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.get_reservation("42")

    assert result.status == PMSStatus.SUCCESS
    reservation = result.data["reservation"]
    assert reservation["guest_name"] == "Alice Dupont"
    assert reservation["arrival_date"] == "2026-09-10"
    assert reservation["room_id"] == "7"
    assert reservation["extras"] == [
        {
            "code": "SERVICE-6",
            "label": "Breakfast",
            "quantity": 2,
            "unit_price": "15.00",
        }
    ]
    assert result.verified is True


def test_adapter_reports_ambiguous_guest_search():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.search_reservations(guest_name="Monsieur Dupont")

    assert result.status == PMSStatus.AMBIGUOUS
    assert len(result.data["reservations"]) == 2


def test_adapter_lists_arrivals_for_a_requested_day():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.list_arrivals(arrival_date=date(2026, 9, 10))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["arrival_date"] == "2026-09-10"
    assert [item["guest_name"] for item in result.data["reservations"]] == [
        "Alice Dupont",
        "Alix Dupont",
    ]


def test_adapter_returns_a_verified_empty_arrivals_list():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.list_arrivals(arrival_date=date(2026, 9, 11))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["reservations"] == []


def test_adapter_lists_departures_for_a_requested_day():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.list_departures(departure_date=date(2026, 9, 12))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["departure_date"] == "2026-09-12"
    assert len(result.data["reservations"]) == 2


def test_adapter_calculates_verified_occupancy_from_rooms_and_stays():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.get_occupancy(stay_date=date(2026, 9, 10))

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data == {
        "stay_date": "2026-09-10",
        "total_rooms": 2,
        "occupied_rooms": 1,
        "available_rooms": 1,
        "occupancy_rate_percent": 50.0,
    }


def test_adapter_calculates_an_occupancy_range_with_one_pms_read_pair():
    client = FakeClient()
    adapter = QloAppsAdapter(client)

    result = adapter.get_occupancy_range(
        start_date=date(2026, 9, 10),
        end_date=date(2026, 9, 12),
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["average_occupancy_rate_percent"] == 33.3
    assert [day["occupied_rooms"] for day in result.data["days"]] == [1, 1, 0]
    assert client.hotel_rooms_calls == 1
    assert client.room_bookings_calls == 1


def test_adapter_previews_then_creates_and_verifies_a_booking():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)
    arguments = {
        "guest_first_name": "Pierre",
        "guest_last_name": "Delafitte",
        "email": "pierre@example.test",
        "phone": "0600000000",
        "arrival_date": date(2026, 9, 10),
        "departure_date": date(2026, 9, 12),
        "adults": 2,
        "children": 0,
        "room_type_id": "2",
        "room_id": "7",
    }

    preview = adapter.preview_create_reservation(**arguments)
    first = adapter.create_reservation(
        **arguments,
        idempotency_key="request-create:call-create",
    )
    second = adapter.create_reservation(
        **arguments,
        idempotency_key="request-create:call-create",
    )

    assert "420.00 EUR TTC" in preview
    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert write_client.create_calls[0]["room_unit_price_without_tax"] == Decimal(
        "175.00"
    )
    assert write_client.create_calls[0]["room_tax_rules_group_id"] == 1
    assert first.data["room_total_with_tax"] == "420.0000"
    assert first.data["reservation"]["guest_name"] == "Pierre Delafitte"
    assert second.status == PMSStatus.SUCCESS
    assert second.verified is True
    assert len(write_client.create_calls) == 1


def test_adapter_recovers_a_created_booking_after_qloapps_returns_an_error():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client, fail_after_write=True)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.create_reservation(
        guest_first_name="Jean",
        guest_last_name="Claude Veceaux",
        email=None,
        phone=None,
        arrival_date=date(2026, 9, 7),
        departure_date=date(2026, 9, 14),
        adults=1,
        children=0,
        room_type_id="2",
        room_id="7",
        idempotency_key="request-long-stay:create",
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.message == "Reservation creee et confirmee par relecture QloApps."
    assert result.data["recovered_after_api_error"] is True
    assert result.data["reservation"]["guest_name"] == "Jean Claude Veceaux"
    assert result.data["room_total_with_tax"] == "1470.0000"
    assert write_client.create_calls[0]["room_unit_price_without_tax"] == Decimal(
        "1050.00"
    )


def test_adapter_uses_explicit_technical_contact_when_real_contact_is_missing():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)
    arguments = {
        "guest_first_name": "Jean",
        "guest_last_name": "Pierrlafittre",
        "email": None,
        "phone": None,
        "arrival_date": date(2026, 9, 8),
        "departure_date": date(2026, 9, 12),
        "adults": 2,
        "children": 0,
        "room_type_id": "2",
        "room_id": "7",
    }

    preview = adapter.preview_create_reservation(**arguments)
    result = adapter.create_reservation(
        **arguments,
        idempotency_key="request-no-contact:call-create",
    )

    sent = write_client.create_calls[0]
    assert "Coordonnees reelles non renseignees" in preview
    assert sent["email"].endswith("@agentpms.invalid")
    assert sent["phone"] == "0000000000"
    assert sent["room_unit_price_without_tax"] == Decimal("525.00")
    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["contact_placeholders"] == {"email": True, "phone": True}


def test_adapter_updates_and_verifies_a_single_room_reservation():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    preview = adapter.preview_update_reservation(
        reservation_id="42",
        arrival_date=None,
        departure_date=date(2026, 9, 14),
        adults=3,
        children=None,
    )
    result = adapter.update_reservation(
        reservation_id="42",
        arrival_date=None,
        departure_date=date(2026, 9, 14),
        adults=3,
        children=None,
        idempotency_key="request:0:update_reservation",
    )

    assert "2026-09-14" in preview
    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["reservation"]["departure_date"] == "2026-09-14"
    assert result.data["reservation"]["adults"] == 3
    assert len(write_client.calls) == 1


def test_adapter_refuses_update_when_the_assigned_room_has_a_conflict():
    read_client = FakeClient()
    read_client.list_room_bookings = lambda hotel_id: [
        {
            "id": 42,
            "id_room": 7,
            "date_from": "2026-09-10",
            "date_to": "2026-09-12",
            "is_cancelled": 0,
            "is_refunded": 0,
        },
        {
            "id": 99,
            "id_room": 7,
            "date_from": "2026-09-13",
            "date_to": "2026-09-16",
            "is_cancelled": 0,
            "is_refunded": 0,
        },
    ]
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.update_reservation(
        reservation_id="42",
        arrival_date=None,
        departure_date=date(2026, 9, 14),
        adults=None,
        children=None,
        idempotency_key="request:0:update_reservation",
    )

    assert result.status == PMSStatus.CONFLICT
    assert result.verified is True
    assert write_client.calls == []


def test_adapter_maps_not_found_as_a_verified_absence():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.get_reservation("999")

    assert result.status == PMSStatus.NOT_FOUND
    assert result.message == "La reservation QloApps #999 n'existe pas."
    assert result.verified is True


def test_adapter_maps_and_filters_availability():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.check_availability(
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 12),
        room_type="Delux",
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["rooms"] == [
        {
            "room_id": "7",
            "room_number": "DR-102",
            "room_type": "Delux Rooms",
            "room_type_id": "2",
            "max_adults": 0,
            "max_children": 0,
            "available": True,
        }
    ]


def test_adapter_excludes_rooms_whose_capacity_is_too_small():
    client = FakeClient()
    for category in client.availability["room_types"]:
        for room in category["rooms"]["available"].values():
            room["max_adult"] = 2
            room["max_children"] = 1
    adapter = QloAppsAdapter(client)

    result = adapter.check_availability(
        arrival_date=date(2026, 10, 15),
        departure_date=date(2026, 10, 19),
        adults=7,
        children=0,
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["rooms"] == []
    assert all(
        category["available_count"] == 0
        for category in result.data["room_types"]
    )


def test_adapter_builds_a_verified_comparative_quote_with_breakfast():
    adapter = QloAppsAdapter(FakeClient())

    result = adapter.quote_stay(
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 12),
        adults=2,
        children=0,
        include_breakfast=True,
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert result.data["breakfast"] == {
        "included": True,
        "service_id": "9",
        "name": "Breakfast",
        "quantity_per_charge": 2,
        "unit_price_excluding_tax": "350.00",
        "tax_rate_percent": "20.00",
        "per_day": True,
        "subtotal_excluding_tax": "1400.00",
        "tax_total": "280.00",
        "total_with_tax": "1680.00",
        "per_night_with_tax": "840.00",
    }
    assert result.data["room_quotes"][0][
        "total_per_night_excluding_tourist_tax"
    ] == "1050.00"
    assert result.data["room_quotes"][0][
        "total_stay_excluding_tourist_tax"
    ] == "2100.00"
    assert result.data["tourist_tax"] == {
        "status": "not_configured",
        "included_in_totals": False,
    }
    assert "Taxe de sejour : non configuree" in result.message


def test_qloapps_write_is_not_exposed_before_validation():
    adapter = QloAppsAdapter(FakeClient())

    assert "add_extra" not in adapter.capabilities()
    assert "create_reservation" not in adapter.capabilities()
    assert "update_reservation" not in adapter.capabilities()
    result = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=Decimal("15.00"),
        idempotency_key="request:call",
    )
    assert result.status == PMSStatus.FORBIDDEN
    assert result.verified is False


def test_write_capability_and_preview_use_qloapps_catalog_price():
    read_client = FakeClient()
    adapter = QloAppsAdapter(
        read_client,
        write_client=FakeWriteClient(read_client),
    )

    assert "add_extra" in adapter.capabilities()
    assert "create_reservation" in adapter.capabilities()
    assert "update_reservation" in adapter.capabilities()
    preview = adapter.preview_add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Petit-dejeuner",
        quantity=1,
        unit_price=None,
    )

    assert "350.00 EUR HT par nuit x 2 nuit(s)" in preview
    assert "TVA 20.00% (140.00 EUR)" in preview
    assert "total 840.00 EUR TTC" in preview


def test_add_extra_is_verified_and_idempotent():
    read_client = FakeClient()
    _remove_existing_services(read_client)
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    first = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=None,
        idempotency_key="request:call",
    )
    second = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=None,
        idempotency_key="request:retry",
    )

    assert first.status == PMSStatus.SUCCESS
    assert first.verified is True
    assert second.status == PMSStatus.SUCCESS
    assert second.verified is True
    assert "aucune duplication" in second.message
    assert len(write_client.calls) == 1
    extras = second.data["reservation"]["extras"]
    assert [extra["code"] for extra in extras].count("SERVICE-9") == 1
    assert extras[-1]["unit_price"] == "350.00"


def test_add_extra_recovers_from_error_after_successful_write():
    read_client = FakeClient()
    _remove_existing_services(read_client)
    write_client = FakeWriteClient(read_client, fail_after_write=True)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.add_extra(
        reservation_id="42",
        extra_code="9",
        label="Breakfast",
        quantity=1,
        unit_price=Decimal("350.00"),
        idempotency_key="request:call",
    )

    assert result.status == PMSStatus.SUCCESS
    assert result.verified is True
    assert "malgre une erreur" in result.message


def test_add_extra_rolls_back_a_service_with_incorrect_tax():
    read_client = FakeClient()
    _remove_existing_services(read_client)
    write_client = FakeWriteClient(
        read_client,
        applied_tax_rate=Decimal("-0.16667"),
    )
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=None,
        idempotency_key="request:call",
    )

    assert result.status == PMSStatus.ERROR
    assert result.error_code == "qloapps_service_price_verification_failed"
    assert "etat initial restaure" in result.message
    assert len(write_client.calls) == 2
    assert read_client.bookings["42"]["associations"]["room_types"][0][
        "rooms"
    ][0]["services"] == []


def test_add_extra_rejects_a_price_not_matching_qloapps():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=Decimal("15.00"),
        idempotency_key="request:call",
    )

    assert result.status == PMSStatus.CONFLICT
    assert result.error_code == "qloapps_service_price_mismatch"
    assert write_client.calls == []


def test_add_extra_fails_closed_when_another_service_already_exists():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=None,
        idempotency_key="request:call",
    )

    assert result.status == PMSStatus.CONFLICT
    assert result.error_code == "qloapps_existing_services_unsupported"
    assert write_client.calls == []


def test_adapter_checks_in_then_checks_out_and_verifies_each_transition():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    checked_in = adapter.check_in_reservation(
        reservation_id="42",
        idempotency_key="request:0:check_in_reservation",
    )
    checked_out = adapter.check_out_reservation(
        reservation_id="42",
        idempotency_key="request:1:check_out_reservation",
    )
    reread = adapter.get_reservation("42")

    assert checked_in.status == PMSStatus.SUCCESS
    assert checked_in.verified is True
    assert checked_in.data["reservation"]["status"] == "checked_in"
    assert checked_out.status == PMSStatus.SUCCESS
    assert checked_out.verified is True
    assert checked_out.data["reservation"]["status"] == "checked_out"
    assert reread.data["reservation"]["status"] == "checked_out"
    assert [call["status"] for call in write_client.room_status_calls] == [2, 3]


def test_adapter_refuses_check_out_before_check_in():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.check_out_reservation(
        reservation_id="42",
        idempotency_key="request:0:check_out_reservation",
    )

    assert result.status == PMSStatus.CONFLICT
    assert result.verified is True
    assert result.error_code == "qloapps_invalid_check_out_transition"
    assert write_client.room_status_calls == []


def test_adapter_issues_an_invoice_then_reads_verified_financial_data():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    before = adapter.get_invoice(reservation_id="42")
    issued = adapter.issue_invoice(
        reservation_id="42",
        idempotency_key="request:0:issue_invoice",
    )
    after = adapter.get_invoice(reservation_id="42")

    assert before.status == PMSStatus.NOT_FOUND
    assert before.verified is True
    assert issued.status == PMSStatus.SUCCESS
    assert issued.verified is True
    assert issued.data["invoice"]["number"] == 1001
    assert issued.data["invoice"]["total_including_tax"] == "420.00"
    assert issued.data["invoice"]["lines"][0]["description"] == "Delux Rooms"
    assert after.data == issued.data
    assert write_client.invoice_calls == [
        {"order_id": "42", "idempotency_key": "request:0:issue_invoice"}
    ]


def test_invoice_recomputes_a_qloapps_service_line_from_unit_price_and_quantity():
    read_client = FakeClient()
    read_client.invoices["1"] = {
        "id": 1,
        "id_order": 42,
        "number": 1001,
        "total_paid_tax_excl": "700.00",
        "total_paid_tax_incl": "840.00",
        "date_add": "2026-09-12 11:00:00",
    }
    read_client.list_order_details = lambda order_id: [
        {
            "id": 8,
            "id_order": 42,
            "id_order_invoice": 1,
            "product_name": "Breakfast",
            "product_quantity": 2,
            "unit_price_tax_excl": "350.00",
            "unit_price_tax_incl": "420.00",
            # Valeurs incoherentes renvoyees par QloApps 1.7 pour ce cas.
            "total_price_tax_excl": "350.00",
            "total_price_tax_incl": "420.00",
        }
    ]

    result = QloAppsAdapter(read_client).get_invoice(reservation_id="42")

    assert result.status == PMSStatus.SUCCESS
    assert result.data["invoice"]["lines"][0]["total_excluding_tax"] == "700.00"
    assert result.data["invoice"]["lines"][0]["total_including_tax"] == "840.00"


def test_adapter_lists_reads_and_replies_to_a_customer_request():
    read_client = FakeClient()
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(
        read_client,
        write_client=write_client,
        employee_id=3,
    )

    listed = adapter.list_customer_requests(status="open")
    loaded = adapter.get_customer_request(request_id="12")
    replied = adapter.reply_customer_request(
        request_id="12",
        message="Oui, la reception sera prevenue.",
        idempotency_key="request:0:reply_customer_request",
    )
    duplicate = adapter.reply_customer_request(
        request_id="12",
        message="Oui, la reception sera prevenue.",
        idempotency_key="request:1:reply_customer_request",
    )

    assert listed.status == PMSStatus.SUCCESS
    assert listed.data["requests"][0]["subject"] == "Arrivee tardive"
    assert loaded.data["request"]["messages"][0]["sender"] == "customer"
    assert replied.status == PMSStatus.SUCCESS
    assert replied.verified is True
    assert replied.data["request"]["messages"][-1]["sender"] == "hotel"
    assert duplicate.status == PMSStatus.SUCCESS
    assert "aucune duplication" in duplicate.message
    assert len(write_client.customer_message_calls) == 1


def test_add_extra_fails_closed_for_multiple_rooms():
    read_client = FakeClient()
    second_room = deepcopy(
        read_client.bookings["42"]["associations"]["room_types"][0]["rooms"][0]
    )
    second_room["id_room"] = 8
    read_client.bookings["42"]["associations"]["room_types"][0]["rooms"].append(
        second_room
    )
    write_client = FakeWriteClient(read_client)
    adapter = QloAppsAdapter(read_client, write_client=write_client)

    result = adapter.add_extra(
        reservation_id="42",
        extra_code="BREAKFAST",
        label="Breakfast",
        quantity=1,
        unit_price=Decimal("350.00"),
        idempotency_key="request:call",
    )

    assert result.status == PMSStatus.AMBIGUOUS
    assert result.error_code == "qloapps_service_room_ambiguous"
    assert write_client.calls == []
