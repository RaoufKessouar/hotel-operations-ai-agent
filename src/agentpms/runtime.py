"""Composition explicite des composants de la V1."""

from __future__ import annotations

from agentpms.agent import AgentOrchestrator, DecisionModel
from agentpms.config import Settings
from agentpms.knowledge import KnowledgeClient
from agentpms.observability.tracing import TraceSink
from agentpms.pms import PMSAdapter, QloAppsAdapter, QloAppsClient
from agentpms.safety import (
    ConfirmationManager,
    PermissionPolicy,
)
from agentpms.safety.grounding import DecisionGroundingPolicy
from agentpms.tools import ToolRegistry
from agentpms.tools.knowledge_tools import SearchKnowledgeTool
from agentpms.tools.pms_tools import (
    AddExtraTool,
    CheckInReservationTool,
    CheckOutReservationTool,
    CheckAvailabilityTool,
    CreateReservationTool,
    CreateReservationsTool,
    GetOccupancyTool,
    GetOccupancyRangeTool,
    GetInvoiceTool,
    GetCustomerRequestTool,
    GetReservationTool,
    GetReservationsTool,
    QueryStaysTool,
    ListArrivalsTool,
    ListDeparturesTool,
    IssueInvoiceTool,
    IssueInvoicesTool,
    ListCustomerRequestsTool,
    PlanRoomAllocationTool,
    QuoteStayTool,
    SearchReservationsTool,
    ReplyCustomerRequestTool,
    UpdateReservationTool,
)


READ_TOOLS = {
    "search_knowledge",
    "search_reservations",
    "get_reservation",
    "get_reservations",
    "query_stays",
    "list_arrivals",
    "list_departures",
    "get_occupancy",
    "get_occupancy_range",
    "check_availability",
    "quote_stay",
    "plan_room_allocation",
    "get_invoice",
    "list_customer_requests",
    "get_customer_request",
}
WRITE_TOOLS = {
    "create_reservation",
    "create_reservations",
    "update_reservation",
    "check_in_reservation",
    "check_out_reservation",
    "issue_invoice",
    "issue_invoices",
    "reply_customer_request",
    "add_extra",
}


def build_registry(
    *, pms: PMSAdapter, knowledge: KnowledgeClient | None = None
) -> ToolRegistry:
    capabilities = pms.capabilities()
    tool_candidates = {
        "search_reservations": SearchReservationsTool(pms),
        "get_reservation": GetReservationTool(pms),
        "list_arrivals": ListArrivalsTool(pms),
        "list_departures": ListDeparturesTool(pms),
        "get_occupancy": GetOccupancyTool(pms),
        "get_occupancy_range": GetOccupancyRangeTool(pms),
        "check_availability": CheckAvailabilityTool(pms),
        "quote_stay": QuoteStayTool(pms),
        "get_invoice": GetInvoiceTool(pms),
        "list_customer_requests": ListCustomerRequestsTool(pms),
        "get_customer_request": GetCustomerRequestTool(pms),
        "create_reservation": CreateReservationTool(pms),
        "update_reservation": UpdateReservationTool(pms),
        "check_in_reservation": CheckInReservationTool(pms),
        "check_out_reservation": CheckOutReservationTool(pms),
        "issue_invoice": IssueInvoiceTool(pms),
        "reply_customer_request": ReplyCustomerRequestTool(pms),
        "add_extra": AddExtraTool(pms),
    }
    tools = [
        tool
        for capability, tool in tool_candidates.items()
        if capability in capabilities
    ]
    if {"check_availability", "quote_stay"} <= capabilities:
        tools.append(PlanRoomAllocationTool(pms))
    if "get_reservation" in capabilities:
        tools.append(GetReservationsTool(pms))
    if "query_stays" in capabilities:
        tools.append(QueryStaysTool(pms))
    if "create_reservation" in capabilities:
        tools.append(CreateReservationsTool(pms))
    if "issue_invoice" in capabilities:
        tools.append(IssueInvoicesTool(pms))
    if knowledge is not None:
        tools.append(SearchKnowledgeTool(knowledge))
    return ToolRegistry(tools)


def build_pms_adapter(settings: Settings) -> PMSAdapter:
    if settings.pms_provider != "qloapps":
        raise ValueError(f"PMS non configure: {settings.pms_provider}")
    if not settings.qloapps_api_key:
        raise ValueError("AGENTPMS_QLOAPPS_API_KEY est requis pour QloApps")
    client = QloAppsClient(
        base_url=settings.qloapps_base_url,
        api_key=settings.qloapps_api_key,
        timeout_seconds=settings.qloapps_timeout_seconds,
    )
    write_client = None
    if settings.qloapps_write_api_key:
        write_client = QloAppsClient(
            base_url=settings.qloapps_base_url,
            api_key=settings.qloapps_write_api_key,
            timeout_seconds=settings.qloapps_timeout_seconds,
        )
    return QloAppsAdapter(
        client,
        write_client=write_client,
        hotel_id=settings.qloapps_hotel_id,
        booking_scan_limit=settings.qloapps_booking_scan_limit,
        employee_id=settings.qloapps_employee_id,
    )


def build_orchestrator(
    *,
    settings: Settings,
    decision_model: DecisionModel,
    pms: PMSAdapter,
    knowledge: KnowledgeClient | None = None,
    trace_sink: TraceSink | None = None,
    decision_grounding: DecisionGroundingPolicy | None = None,
) -> AgentOrchestrator:
    registry = build_registry(pms=pms, knowledge=knowledge)
    available = {spec.name for spec in registry.specs()}
    permissions = PermissionPolicy(
        {
            "viewer": READ_TOOLS & available,
            "reception": (READ_TOOLS | WRITE_TOOLS) & available,
            "manager": (READ_TOOLS | WRITE_TOOLS) & available,
        }
    )
    return AgentOrchestrator(
        decision_model=decision_model,
        registry=registry,
        permissions=permissions,
        confirmations=ConfirmationManager(settings.confirmation_ttl_seconds),
        decision_grounding=decision_grounding,
        trace_sink=trace_sink,
        max_steps=settings.max_steps,
    )
