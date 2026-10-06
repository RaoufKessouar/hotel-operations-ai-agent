from datetime import date

from agentpms.agent import LocalRulesDecisionModel
from agentpms.config import Settings
from agentpms.contracts import AgentRunStatus
from agentpms.runtime import build_orchestrator


def _settings():
    return Settings(
        rag_project=None,
        knowledge_url="http://127.0.0.1:8765/knowledge/query",
        llm_base_url="http://127.0.0.1:8000/v1",
        llm_api_key="EMPTY",
        llm_model="model",
        max_steps=6,
        confirmation_ttl_seconds=300,
    )


def test_local_rules_searches_a_guest_then_summarizes(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    response = orchestrator.start(
        "Trouve la réservation de Jean Dupont",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert "Jean Dupont" in response.message
    assert response.tool_results[0].tool_name == "search_reservations"


def test_local_rules_resumes_after_an_ambiguous_search(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )
    ambiguous = orchestrator.start(
        "Cherche les réservations Dupont",
        actor_id="reception-1",
    )

    assert ambiguous.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "R-4587" in ambiguous.message
    completed = orchestrator.continue_request(
        request_id=ambiguous.request_id,
        user_message="La réservation R-4587",
        actor_id="reception-1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert "Jean Dupont" in completed.message


def test_local_rules_prepares_qloapps_extra_without_inventing_a_price(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    response = orchestrator.start(
        "Ajoute un petit-déjeuner à la réservation R-4587",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.NEEDS_CONFIRMATION
    state = orchestrator.state_store.get(response.request_id)
    assert "unit_price" not in state.pending_call.arguments


def test_local_rules_lists_all_arrivals_of_the_day(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
    )

    response = orchestrator.start(
        "quelle sont toutes les arrivées du jours?",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert "Jean Dupont" in response.message
    assert response.tool_results[0].tool_name == "list_arrivals"
    assert response.tool_results[0].verified is True


def test_local_rules_lists_departures_for_a_requested_day(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
    )

    response = orchestrator.start(
        "Quels sont les departs du 14/09 ?",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert response.tool_results[0].tool_name == "list_departures"
    assert "Jean Dupont" in response.message


def test_local_rules_prolongs_a_reservation_after_confirmation(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
    )

    pending = orchestrator.start(
        "Prolonge la reservation R-4587 jusqu'au 15/09",
        actor_id="reception-1",
    )

    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert pending.confirmation.tool_name == "update_reservation"
    assert pms.get_reservation("R-4587").data["reservation"][
        "departure_date"
    ] == "2026-09-14"

    completed = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert pms.get_reservation("R-4587").data["reservation"][
        "departure_date"
    ] == "2026-09-15"


def test_local_rules_checks_in_then_checks_out_with_confirmation(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    check_in_pending = orchestrator.start(
        "Fais le check-in de la reservation R-4587",
        actor_id="reception-1",
    )
    assert check_in_pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert check_in_pending.confirmation.tool_name == "check_in_reservation"

    checked_in = orchestrator.confirm(
        request_id=check_in_pending.request_id,
        confirmation_id=check_in_pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    assert checked_in.status == AgentRunStatus.COMPLETED
    assert pms.get_reservation("R-4587").data["reservation"]["status"] == "checked_in"

    check_out_pending = orchestrator.continue_request(
        request_id=check_in_pending.request_id,
        user_message="Fais maintenant le check-out de la reservation R-4587",
        actor_id="reception-1",
    )
    assert check_out_pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert check_out_pending.confirmation.tool_name == "check_out_reservation"

    checked_out = orchestrator.confirm(
        request_id=check_out_pending.request_id,
        confirmation_id=check_out_pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    assert checked_out.status == AgentRunStatus.COMPLETED
    assert pms.get_reservation("R-4587").data["reservation"]["status"] == "checked_out"


def test_local_rules_issues_then_consults_an_invoice(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    pending = orchestrator.start(
        "Edite la facture de la reservation R-4587",
        actor_id="reception-1",
    )
    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert pending.confirmation.tool_name == "issue_invoice"

    completed = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    assert completed.status == AgentRunStatus.COMPLETED
    assert "Facture #" in completed.message

    consulted = orchestrator.continue_request(
        request_id=pending.request_id,
        user_message="Consulte la facture de la reservation R-4587",
        actor_id="reception-1",
    )
    assert consulted.status == AgentRunStatus.COMPLETED
    assert consulted.tool_results[-1].tool_name == "get_invoice"
    assert "R-4587" in consulted.message


def test_local_rules_lists_and_replies_to_customer_requests_after_confirmation(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    listed = orchestrator.start(
        "Liste les demandes clients ouvertes",
        actor_id="reception-1",
    )
    assert listed.status == AgentRunStatus.COMPLETED
    assert "Arrivee tardive" in listed.message
    assert listed.tool_results[-1].tool_name == "list_customer_requests"

    pending = orchestrator.continue_request(
        request_id=listed.request_id,
        user_message=(
            "Reponds a la demande client 12 : Oui, nous vous attendrons apres 22 h."
        ),
        actor_id="reception-1",
    )
    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert pending.confirmation.tool_name == "reply_customer_request"
    assert "nous vous attendrons" in pending.confirmation.summary

    completed = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    assert completed.status == AgentRunStatus.COMPLETED
    assert "relue avec succes" in completed.message


def test_local_rules_understands_resa_and_a_misspelled_title(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
    )

    response = orchestrator.start(
        "trouve la resa de monisuer Pierre dupice",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert "Aucune réservation" in response.message
    assert response.tool_results[0].tool_name == "search_reservations"
    assert response.tool_results[0].status.value == "not_found"


def test_local_rules_understands_arrival_count_for_this_evening(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
    )

    response = orchestrator.start(
        "quelle est le nombre d arrivée pour ce soir?",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert "1 arrivée" in response.message
    assert "Jean Dupont" in response.message
    assert response.tool_results[0].tool_name == "list_arrivals"


def test_local_rules_understands_misspelled_occupancy_for_this_evening(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
    )

    response = orchestrator.start(
        "quelle est le taux d occupatoin pour ce soir ?",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert "50,0 %" in response.message
    assert "1 chambre occupée sur 2" in response.message
    assert response.tool_results[0].tool_name == "get_occupancy"
    assert response.tool_results[0].verified is True


def test_local_rules_keeps_occupancy_context_for_month_and_tomorrow(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 2)
        ),
        pms=pms,
    )
    first = orchestrator.start(
        "quelle est le taux d occupation pour ce soir?",
        actor_id="reception-1",
    )

    month = orchestrator.continue_request(
        request_id=first.request_id,
        user_message="et pour le reste du mois de septembre?",
        actor_id="reception-1",
    )
    tomorrow = orchestrator.continue_request(
        request_id=first.request_id,
        user_message="et pour demain?",
        actor_id="reception-1",
    )

    assert first.status == AgentRunStatus.COMPLETED
    assert month.status == AgentRunStatus.COMPLETED
    assert "2026-09-03 au 2026-09-30" in month.message
    assert "7,1 %" in month.message
    assert month.tool_results[-1].tool_name == "get_occupancy_range"
    assert tomorrow.status == AgentRunStatus.COMPLETED
    assert "2026-09-03" in tomorrow.message
    assert tomorrow.tool_results[-1].tool_name == "get_occupancy"


def test_local_rules_calculates_a_complete_named_month(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 4)
        ),
        pms=pms,
    )

    response = orchestrator.start(
        "Quel est le taux d'occupation pour le mois de septembre ?",
        actor_id="reception-1",
    )

    assert response.status == AgentRunStatus.COMPLETED
    assert response.tool_results[-1].tool_name == "get_occupancy_range"
    assert response.tool_results[-1].data["start_date"] == "2026-09-01"
    assert response.tool_results[-1].data["end_date"] == "2026-09-30"


def test_local_rules_understands_misspelled_tomorrow_and_ddmm_followup(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 3)
        ),
        pms=pms,
    )

    tomorrow = orchestrator.start(
        "quelle est le taux d occupation pour demian?",
        actor_id="reception-1",
    )
    september_tenth = orchestrator.continue_request(
        request_id=tomorrow.request_id,
        user_message="et pour le 10/09",
        actor_id="reception-1",
    )

    assert tomorrow.status == AgentRunStatus.COMPLETED
    assert "2026-09-04" in tomorrow.message
    assert tomorrow.tool_results[-1].tool_name == "get_occupancy"
    assert september_tenth.status == AgentRunStatus.COMPLETED
    assert "2026-09-10" in september_tenth.message
    assert september_tenth.tool_results[-1].tool_name == "get_occupancy"


def test_local_rules_keeps_arrivals_context_for_tomorrow(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 11)
        ),
        pms=pms,
    )
    first = orchestrator.start(
        "combien d arrivées ce soir?",
        actor_id="reception-1",
    )

    tomorrow = orchestrator.continue_request(
        request_id=first.request_id,
        user_message="et pour demain?",
        actor_id="reception-1",
    )

    assert tomorrow.status == AgentRunStatus.COMPLETED
    assert "Jean Dupont" in tomorrow.message
    assert tomorrow.tool_results[-1].tool_name == "list_arrivals"


def test_local_rules_prepares_a_booking_then_breakfast_with_two_confirmations(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 2)
        ),
        pms=pms,
    )

    missing_details = orchestrator.start(
        (
            "cree une resa pour pierre delafitte de ce soir jusqua 10/09 "
            "pour 2 personnes avec petit dej"
        ),
        actor_id="reception-1",
    )

    assert missing_details.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "double" in missing_details.message
    assert "adresse email" not in missing_details.message
    assert missing_details.tool_results[-1].tool_name == "check_availability"

    booking_confirmation = orchestrator.continue_request(
        request_id=missing_details.request_id,
        user_message=(
            "La chambre double, email pierre.delafitte@agentpms.test, "
            "tel 0600000000"
        ),
        actor_id="reception-1",
    )

    assert booking_confirmation.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert "Pierre Delafitte" in booking_confirmation.confirmation.summary
    assert pms.search_reservations(guest_name="Pierre Delafitte").status.value == "not_found"

    breakfast_confirmation = orchestrator.confirm(
        request_id=booking_confirmation.request_id,
        confirmation_id=booking_confirmation.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert breakfast_confirmation.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert breakfast_confirmation.confirmation.tool_name == "add_extra"
    created = pms.search_reservations(guest_name="Pierre Delafitte")
    assert created.status.value == "success"
    reservation_id = created.data["reservation"]["reservation_id"]
    assert created.data["reservation"]["extras"] == []

    completed = orchestrator.confirm(
        request_id=breakfast_confirmation.request_id,
        confirmation_id=breakfast_confirmation.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    reservation = pms.get_reservation(reservation_id).data["reservation"]
    assert reservation["guest_name"] == "Pierre Delafitte"
    assert reservation["extras"][0]["code"] == "BREAKFAST"


def test_local_rules_continues_a_misspelled_make_booking_request(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 3)
        ),
        pms=pms,
    )

    missing_guests = orchestrator.start(
        "fait une resatvation au nom de Marc Kuber pour le 03/09 au 10/09",
        actor_id="reception-1",
    )

    assert missing_guests.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "nombre de personnes" in missing_guests.message

    missing_room = orchestrator.continue_request(
        request_id=missing_guests.request_id,
        user_message="2 personnes, email marc.kuber@agentpms.test, tel 0600000001",
        actor_id="reception-1",
    )

    assert missing_room.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "type de chambre" in missing_room.message
    assert missing_room.tool_results[-1].tool_name == "check_availability"
    assert pms.search_reservations(guest_name="Marc Kuber").status.value == "not_found"


def test_local_rules_keeps_and_corrects_a_guest_name_during_booking(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 3)
        ),
        pms=pms,
    )

    missing_guests = orchestrator.start(
        "fait une resa du 04/09 au 09/09 au nom de pieceau Jean",
        actor_id="reception-1",
    )
    assert missing_guests.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "prenom et le nom" not in missing_guests.message
    assert "nombre de personnes" in missing_guests.message

    missing_contact = orchestrator.continue_request(
        request_id=missing_guests.request_id,
        user_message="Pieceau Jean, 2 personnes",
        actor_id="reception-1",
    )
    assert missing_contact.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "type de chambre" in missing_contact.message
    assert "adresse email" not in missing_contact.message

    corrected_name = orchestrator.continue_request(
        request_id=missing_guests.request_id,
        user_message="Jean Pieceau",
        actor_id="reception-1",
    )
    assert corrected_name.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "type de chambre" in corrected_name.message

    pending = orchestrator.continue_request(
        request_id=missing_guests.request_id,
        user_message=(
            "chambre double, email jean.pieceau@agentpms.test, tel 0600000002"
        ),
        actor_id="reception-1",
    )
    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert "Jean Pieceau" in pending.confirmation.summary
    assert pms.search_reservations(guest_name="Jean Pieceau").status.value == "not_found"


def test_local_rules_accepts_an_explicitly_missing_contact(pms):
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 3)
        ),
        pms=pms,
    )

    missing_room = orchestrator.start(
        (
            "cree une reservation pour Jean Pierrlafittre du 08/09 au 12/09 "
            "pour 2 personnes sans contact"
        ),
        actor_id="reception-1",
    )
    assert missing_room.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert "type de chambre" in missing_room.message
    assert "adresse email" not in missing_room.message
    assert "telephone" not in missing_room.message

    pending = orchestrator.continue_request(
        request_id=missing_room.request_id,
        user_message="on prend la chambre double",
        actor_id="reception-1",
    )

    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert pending.confirmation.tool_name == "create_reservation"
