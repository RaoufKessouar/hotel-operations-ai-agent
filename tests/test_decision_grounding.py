from agentpms.agent.state import AgentState
from agentpms.contracts import (
    AgentDecision,
    AgentWorkingMemory,
    DecisionKind,
    ToolResult,
    ToolStatus,
)
from agentpms.safety import DecisionGroundingPolicy


def _availability(*, room_id="1", adults=2, children=0):
    return ToolResult(
        call_id="availability-1",
        tool_name="check_availability",
        status=ToolStatus.SUCCESS,
        message="Chambres disponibles.",
        data={
            "arrival_date": "2026-09-08",
            "departure_date": "2026-09-12",
            "adults": adults,
            "children": children,
            "room_types": [
                {
                    "room_type_id": "1",
                    "name": "General Rooms",
                    "available_count": 1,
                    "total_price_with_tax": "6000.00",
                }
            ],
            "rooms": [
                {
                    "room_id": room_id,
                    "room_type_id": "1",
                    "available": True,
                }
            ],
        },
        verified=True,
    )


def _availability_with_multiple_categories():
    result = _availability()
    return result.model_copy(
        update={
            "data": {
                **result.data,
                "currency": "EUR",
                "room_types": [
                    {
                        "room_type_id": "1",
                        "name": "General Rooms",
                        "available_count": 1,
                        "total_price_with_tax": "6000.00",
                    },
                    {
                        "room_type_id": "4",
                        "name": "Luxury Rooms",
                        "available_count": 1,
                        "total_price_with_tax": "13200.00",
                    },
                ],
                "rooms": [
                    {"room_id": "1", "room_type_id": "1", "available": True},
                    {"room_id": "16", "room_type_id": "4", "available": True},
                ],
            }
        }
    )


def _reservation_result():
    return ToolResult(
        call_id="reservation-1",
        tool_name="get_reservation",
        status=ToolStatus.SUCCESS,
        message="Reservation trouvee.",
        data={"reservation": {"reservation_id": "42"}},
        verified=True,
    )


def _reservations_result(*reservation_ids):
    return ToolResult(
        call_id="reservations-1",
        tool_name="get_reservations",
        status=ToolStatus.SUCCESS,
        message="Reservations trouvees.",
        data={
            "reservations": [
                {"reservation_id": str(reservation_id)}
                for reservation_id in reservation_ids
            ]
        },
        verified=True,
    )


def _customer_request_result():
    return ToolResult(
        call_id="request-1",
        tool_name="get_customer_request",
        status=ToolStatus.SUCCESS,
        message="Demande trouvee.",
        data={"request": {"request_id": "12"}},
        verified=True,
    )


def _state(*, results=None, memory=None, room_selection_strategy=None):
    return AgentState(
        actor_id="reception",
        role="reception",
        user_message="formulation libre geree par le modele",
        messages=[{"role": "user", "content": "formulation libre"}],
        tool_results=results or [],
        working_memory=memory or AgentWorkingMemory(),
        room_selection_strategy=room_selection_strategy,
    )


def _create_decision(**overrides):
    arguments = {
        "guest_first_name": "Jean",
        "guest_last_name": "Pierrlafittre",
        "email": None,
        "phone": None,
        "arrival_date": "2026-09-08",
        "departure_date": "2026-09-12",
        "adults": 2,
        "children": 0,
        "room_type_id": "1",
        "room_id": "1",
    }
    arguments.update(overrides)
    return AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="create_reservation",
        arguments=arguments,
    )


def test_create_reservation_requires_matching_verified_availability():
    reviewed = DecisionGroundingPolicy().review(_state(), _create_decision())

    assert reviewed.tool_name == "check_availability"
    assert reviewed.arguments == {
        "arrival_date": "2026-09-08",
        "departure_date": "2026-09-12",
        "room_type": None,
        "adults": 2,
        "children": 0,
    }


def test_create_reservation_accepts_a_room_from_matching_availability():
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability()]),
        _create_decision(),
    )

    assert reviewed.tool_name == "create_reservation"
    assert reviewed.arguments["room_id"] == "1"


def test_create_reservation_asks_for_a_category_professionally_by_default():
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability_with_multiple_categories()]),
        _create_decision(),
    )

    assert reviewed.kind == DecisionKind.CLARIFY
    assert "Quelle categorie souhaitez-vous retenir" in reviewed.response
    assert "moins chere ou la plus chere" not in reviewed.response


def test_create_reservation_can_be_configured_to_choose_the_cheapest():
    reviewed = DecisionGroundingPolicy(
        default_room_selection_strategy="cheapest"
    ).review(
        _state(results=[_availability_with_multiple_categories()]),
        _create_decision(),
    )

    assert reviewed.kind == DecisionKind.TOOL
    assert reviewed.arguments["room_type_id"] == "1"
    assert reviewed.arguments["room_id"] == "1"


def test_verified_price_strategy_selects_the_correct_room_structurally():
    reviewed = DecisionGroundingPolicy().review(
        _state(
            results=[_availability_with_multiple_categories()],
            room_selection_strategy="most_expensive",
        ),
        _create_decision().model_copy(
            update={"room_selection_strategy": "most_expensive"}
        ),
    )

    assert reviewed.kind == DecisionKind.TOOL
    assert reviewed.arguments["room_type_id"] == "4"
    assert reviewed.arguments["room_id"] == "16"


def test_create_reservation_rechecks_a_room_not_returned_by_the_pms():
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability(room_id="2")]),
        _create_decision(room_id="99"),
    )

    assert reviewed.tool_name == "check_availability"


def test_create_reservation_rechecks_availability_for_different_occupants():
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability(adults=1)]),
        _create_decision(adults=2),
    )

    assert reviewed.tool_name == "check_availability"
    assert reviewed.arguments["adults"] == 2


def test_contacts_not_present_in_model_memory_are_not_sent_to_the_pms():
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability()]),
        _create_decision(email="invente@example.com", phone="0612345678"),
    )

    assert reviewed.arguments["email"] is None
    assert reviewed.arguments["phone"] is None


def test_contacts_explicitly_kept_in_working_memory_are_accepted():
    memory = AgentWorkingMemory(
        objective="Creer la reservation",
        known_facts={"email": "jean@test.fr", "phone": "0612345678"},
    )
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability()], memory=memory),
        _create_decision(email="jean@test.fr", phone="0612345678"),
    )

    assert reviewed.arguments["email"] == "jean@test.fr"
    assert reviewed.arguments["phone"] == "0612345678"


def test_unavailable_contacts_are_forced_to_null_from_structured_memory():
    memory = AgentWorkingMemory(unavailable_fields=["email", "phone"])
    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability()], memory=memory),
        _create_decision(email="invente@example.com", phone="0612345678"),
    )

    assert reviewed.arguments["email"] is None
    assert reviewed.arguments["phone"] is None


def test_reservation_write_first_loads_the_target_entity():
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="update_reservation",
        arguments={"reservation_id": "42", "departure_date": "2026-09-15"},
    )

    reviewed = DecisionGroundingPolicy().review(_state(), decision)

    assert reviewed.tool_name == "get_reservation"
    assert reviewed.arguments == {"reservation_id": "42"}


def test_reservation_write_is_accepted_after_verified_read():
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="check_in_reservation",
        arguments={"reservation_id": "42"},
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_reservation_result()]),
        decision,
    )

    assert reviewed == decision


def test_add_extra_uses_the_internal_catalog_after_loading_reservation():
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="add_extra",
        arguments={
            "reservation_id": "42",
            "extra_code": "valeur_du_modele",
            "label": "valeur du modele",
            "quantity": 2,
            "unit_price": "999.00",
        },
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_reservation_result()]),
        decision,
    )

    assert reviewed.arguments["extra_code"] == "BREAKFAST"
    assert reviewed.arguments["label"] == "Petit-dejeuner"
    assert "unit_price" not in reviewed.arguments


def test_add_extra_reuses_the_quantity_from_the_verified_stay_quote():
    reservation = _reservation_result().model_copy(
        update={
            "data": {
                "reservation": {
                    "reservation_id": "42",
                    "arrival_date": "2026-10-05",
                    "departure_date": "2026-10-15",
                    "adults": 1,
                }
            }
        }
    )
    quote = ToolResult(
        call_id="quote-1",
        tool_name="quote_stay",
        status=ToolStatus.SUCCESS,
        message="Devis avec petit-dejeuner.",
        data={
            "arrival_date": "2026-10-05",
            "departure_date": "2026-10-15",
            "adults": 1,
            "breakfast": {
                "included": True,
                "quantity_per_charge": 1,
                "per_day": True,
                "total_with_tax": "4200.00",
            },
        },
        verified=True,
    )
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="add_extra",
        arguments={
            "reservation_id": "42",
            "extra_code": "BREAKFAST",
            "label": "Petit-dejeuner",
            "quantity": 10,
        },
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[reservation, quote]),
        decision,
    )

    assert reviewed.arguments["quantity"] == 1


def test_customer_reply_first_loads_the_exact_thread():
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="reply_customer_request",
        arguments={"request_id": "12", "message": "Oui, bien sur."},
    )

    reviewed = DecisionGroundingPolicy().review(_state(), decision)
    accepted = DecisionGroundingPolicy().review(
        _state(results=[_customer_request_result()]),
        decision,
    )

    assert reviewed.tool_name == "get_customer_request"
    assert accepted == decision


def test_final_and_clarification_are_not_semantically_rewritten():
    clarification = AgentDecision(
        kind=DecisionKind.CLARIFY,
        response="Quelle date souhaitez-vous ?",
    )
    final = AgentDecision(kind=DecisionKind.FINAL, response="Operation terminee.")
    policy = DecisionGroundingPolicy()

    assert policy.review(_state(), clarification) == clarification
    assert policy.review(_state(), final) == final
    assert policy.recover(_state(results=[_availability()])) is None


def test_group_distribution_question_is_not_replaced_by_an_old_room_offer():
    state = _state(results=[_availability_with_multiple_categories()])
    state.workflow_plan = ["create_reservations"]
    clarification = AgentDecision(
        kind=DecisionKind.CLARIFY,
        response="Combien de chambres souhaitez-vous et avec quelle repartition ?",
    )

    reviewed = DecisionGroundingPolicy().review(state, clarification)

    assert reviewed == clarification


def test_unverified_monetary_reformatting_is_replaced_by_the_tool_message():
    availability = _availability().model_copy(
        update={
            "message": "Montant QloApps exact : 6000.00 EUR TTC.",
        }
    )
    decision = AgentDecision(
        kind=DecisionKind.FINAL,
        response="Le total est de 600,00 EUR.",
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[availability]),
        decision,
    )

    assert reviewed.response == "Montant QloApps exact : 6000.00 EUR TTC."


def test_exact_verified_monetary_value_can_be_rephrased():
    decision = AgentDecision(
        kind=DecisionKind.FINAL,
        response="Le total verifie est de 6 000,00 EUR TTC.",
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[_availability()]),
        decision,
    )

    assert reviewed == decision


def test_multiple_reservations_reuse_every_verified_allocated_room():
    allocation = ToolResult(
        call_id="allocation-1",
        tool_name="plan_room_allocation",
        status=ToolStatus.SUCCESS,
        message="Allocation verifiee.",
        data={
            "arrival_date": "2026-09-10",
            "departure_date": "2026-09-11",
            "requested_count": 2,
            "allocations": [
                {
                    "reference": "EGC-1",
                    "adults": 1,
                    "children": 0,
                    "room_id": "21",
                    "room_type_id": "2",
                },
                {
                    "reference": "EGC-2",
                    "adults": 1,
                    "children": 0,
                    "room_id": "22",
                    "room_type_id": "3",
                },
            ],
        },
        verified=True,
    )
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="create_reservations",
        arguments={
            "reservations": [
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
                    "room_type_id": "invente",
                    "room_id": "invente-1",
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
                    "room_type_id": "invente",
                    "room_id": "invente-2",
                },
            ]
        },
    )

    reviewed = DecisionGroundingPolicy().review(
        _state(results=[allocation]),
        decision,
    )

    assert reviewed.tool_name == "create_reservations"
    assert [
        (item["room_type_id"], item["room_id"])
        for item in reviewed.arguments["reservations"]
    ] == [("2", "21"), ("3", "22")]


def test_multiple_invoices_verify_unknown_reservation_ids_before_writing():
    created = ToolResult(
        call_id="batch-1",
        tool_name="create_reservations",
        status=ToolStatus.SUCCESS,
        message="Deux reservations creees.",
        data={
            "reservations": [
                {"reservation_id": "101"},
                {"reservation_id": "102"},
            ]
        },
        verified=True,
    )
    valid = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="issue_invoices",
        arguments={"reservation_ids": ["101", "102"]},
    )
    invented = valid.model_copy(
        update={"arguments": {"reservation_ids": ["101", "999"]}}
    )
    policy = DecisionGroundingPolicy()

    assert policy.review(_state(results=[created]), valid) == valid
    verification = policy.review(_state(results=[created]), invented)
    assert verification.kind == DecisionKind.TOOL
    assert verification.tool_name == "get_reservations"
    assert verification.arguments == {"reservation_ids": ["999"]}
    assert "issue_invoices" in verification.plan

    assert (
        policy.review(
            _state(results=[created, _reservations_result("999")]),
            invented,
        )
        == invented
    )
