const elements = {
  composer: document.querySelector("#composer"),
  input: document.querySelector("#messageInput"),
  send: document.querySelector("#sendButton"),
  messages: document.querySelector("#messages"),
  template: document.querySelector("#messageTemplate"),
  connection: document.querySelector("#connectionStatus"),
  capabilities: document.querySelector("#capabilities"),
  mode: document.querySelector("#decisionMode"),
  pmsStatus: document.querySelector("#pmsStatus"),
  activity: document.querySelector("#activityList"),
  stepCount: document.querySelector("#stepCount"),
  reset: document.querySelector("#newConversation"),
};

const state = {
  actorId: localStorage.getItem("agentpms_actor_id") || crypto.randomUUID(),
  requestId: null,
  awaitingClarification: false,
  awaitingConfirmation: false,
  busy: false,
};
localStorage.setItem("agentpms_actor_id", state.actorId);

const toolLabels = {
  search_reservations: "Recherche de réservation",
  get_reservation: "Lecture de réservation",
  get_reservations: "Lecture de plusieurs réservations",
  query_stays: "Recherche de séjours",
  list_arrivals: "Liste des arrivées",
  list_departures: "Liste des départs",
  get_occupancy: "Calcul de l’occupation",
  get_occupancy_range: "Occupation sur une période",
  check_availability: "Vérification des disponibilités",
  quote_stay: "Calcul du devis",
  plan_room_allocation: "Allocation des chambres",
  create_reservation: "Création de réservation",
  create_reservations: "Création de plusieurs réservations",
  update_reservation: "Modification de réservation",
  check_in_reservation: "Enregistrement du check-in",
  check_out_reservation: "Enregistrement du check-out",
  get_invoice: "Consultation de facture",
  issue_invoice: "Émission de facture",
  issue_invoices: "Émission de plusieurs factures",
  list_customer_requests: "Liste des demandes clients",
  get_customer_request: "Lecture d’une demande client",
  reply_customer_request: "Réponse à un client",
  add_extra: "Ajout de service",
  search_knowledge: "Recherche documentaire",
};

function addMessage(role, text, options = {}) {
  const node = elements.template.content.firstElementChild.cloneNode(true);
  node.classList.add(role === "user" ? "user-message" : "assistant-message");
  if (options.loading) node.classList.add("loading-message");
  node.querySelector(".avatar").textContent = role === "user" ? "R" : "A";
  node.querySelector(".message-author").textContent = role === "user" ? "Réception" : "AgentPMS";
  node.querySelector(".message-text").textContent = text;
  elements.messages.appendChild(node);
  elements.messages.scrollTop = elements.messages.scrollHeight;
  return node;
}

function setBusy(value) {
  state.busy = value;
  elements.send.disabled = value || state.awaitingConfirmation;
  elements.input.disabled = value || state.awaitingConfirmation;
}

function closeConfirmationCards(message = "Cette validation n’est plus active.") {
  document.querySelectorAll(".confirmation-card:not(.confirmation-closed)").forEach((card) => {
    card.classList.add("confirmation-closed");
    card.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    const status = document.createElement("p");
    status.className = "confirmation-status";
    status.textContent = message;
    card.appendChild(status);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Erreur HTTP ${response.status}`);
  }
  return response.json();
}

function renderSources(container, results) {
  const citations = results.flatMap((result) => result.citations || []);
  if (!citations.length) return;
  const list = document.createElement("ul");
  list.className = "sources";
  citations.forEach((citation) => {
    const item = document.createElement("li");
    if (citation.url) {
      const link = document.createElement("a");
      link.href = citation.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = citation.title;
      item.appendChild(link);
    } else {
      item.textContent = citation.title;
    }
    list.appendChild(item);
  });
  container.appendChild(list);
}

function renderConfirmation(container, response) {
  closeConfirmationCards("Remplacée par la validation la plus récente.");
  const confirmation = response.confirmation;
  const card = document.createElement("section");
  card.className = "confirmation-card";
  card.innerHTML = `
    <span class="confirmation-label">Validation requise</span>
    <p></p>
    <div class="confirmation-actions">
      <button class="confirm-button" type="button">Confirmer l’action</button>
      <button class="cancel-button" type="button">Annuler</button>
    </div>
    <p class="confirmation-expiry"></p>`;
  card.querySelector("p").textContent = confirmation.summary;
  const expiry = card.querySelector(".confirmation-expiry");
  expiry.textContent = `Valable jusqu’à ${new Date(confirmation.expires_at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}.`;
  const confirm = card.querySelector(".confirm-button");
  const cancel = card.querySelector(".cancel-button");
  const act = async (approved) => {
    if (state.busy || card.classList.contains("confirmation-closed")) return;
    confirm.disabled = true;
    cancel.disabled = true;
    card.classList.add("confirmation-closed");
    state.awaitingConfirmation = false;
    await submitConfirmation(
      response.request_id,
      confirmation.confirmation_id,
      approved,
    );
  };
  confirm.addEventListener("click", () => act(true));
  cancel.addEventListener("click", () => act(false));
  container.appendChild(card);
}

function applyResponse(response) {
  state.requestId = response.request_id;
  state.awaitingClarification = response.status === "needs_clarification";
  state.awaitingConfirmation = response.status === "needs_confirmation" && Boolean(response.confirmation);
  if (!state.awaitingConfirmation) closeConfirmationCards();
  const displayMessage = response.status === "needs_confirmation"
    ? "L’action est prête. Vérifiez son montant et ses paramètres avant de décider."
    : response.message;
  const message = addMessage("assistant", displayMessage);
  const extra = message.querySelector(".message-extra");
  renderSources(extra, response.tool_results || []);
  if (response.status === "needs_confirmation" && response.confirmation) {
    renderConfirmation(extra, response);
  }
  setBusy(false);
  refreshTrace();
}

async function sendMessage(message) {
  addMessage("user", message);
  const loading = addMessage("assistant", "Analyse de la demande", { loading: true });
  setBusy(true);
  try {
    const path = state.requestId
      ? `/api/requests/${state.requestId}/messages`
      : "/api/requests";
    const response = await api(path, {
      method: "POST",
      body: JSON.stringify({ message, actor_id: state.actorId }),
    });
    loading.remove();
    applyResponse(response);
  } catch (error) {
    loading.remove();
    addMessage("assistant", "Je n’ai pas pu traiter la demande. Vérifiez que les services locaux sont démarrés.");
    console.error(error);
  } finally {
    setBusy(false);
    elements.input.focus();
  }
}

async function submitConfirmation(requestId, confirmationId, approved) {
  const loading = addMessage("assistant", approved ? "Exécution et vérification" : "Annulation", { loading: true });
  setBusy(true);
  try {
    const response = await api(`/api/requests/${requestId}/confirmation`, {
      method: "POST",
      body: JSON.stringify({
        confirmation_id: confirmationId,
        approved,
        actor_id: state.actorId,
      }),
    });
    loading.remove();
    applyResponse(response);
  } catch (error) {
    loading.remove();
    addMessage("assistant", "La confirmation n’a pas pu être appliquée. Aucune réussite n’est supposée.");
    console.error(error);
  } finally {
    setBusy(false);
  }
}

function activityItem(event) {
  const item = document.createElement("li");
  const status = event.status || "info";
  item.className = `activity-item ${status === "error" ? "error" : status === "pending" ? "pending" : ""}`;
  const labels = {
    request_started: "Demande reçue",
    request_continued: "Précision reçue",
    decision: event.tool_name ? `Décision · ${toolLabels[event.tool_name] || event.tool_name}` : "Décision de l’agent",
    confirmation_requested: "Confirmation demandée",
    confirmation_expired: "Confirmation expirée",
    duplicate_write_blocked: "Doublon bloqué",
    plan_created: "Plan d’action créé",
    plan_adapted: "Plan adapté au résultat",
    confirmation_denied: "Action annulée",
    tool_finished: toolLabels[event.tool_name] || "Outil terminé",
    decision_error: "Décision interrompue",
    step_limit_reached: "Limite d’étapes atteinte",
  };
  const title = labels[event.event_type] || event.event_type;
  let detail = event.event_type === "tool_finished"
    ? `Résultat : ${status}${event.fields?.verified ? " · vérifié dans le PMS" : ""}`
    : status === "pending" ? "En attente de votre validation" : "Étape enregistrée";
  if (event.event_type === "plan_created" && event.fields?.tools) {
    detail = event.fields.tools.map((name) => toolLabels[name] || name).join(" → ");
  } else if (event.event_type === "plan_adapted") {
    detail = event.status === "blocked"
      ? "Le résultat impose une nouvelle décision"
      : "La suite des outils a été révisée";
  }
  item.innerHTML = `<strong></strong><p></p><div class="activity-meta"></div>`;
  item.querySelector("strong").textContent = title;
  item.querySelector("p").textContent = detail;
  const meta = item.querySelector(".activity-meta");
  if (event.duration_ms != null) {
    const duration = document.createElement("span");
    duration.textContent = `${Math.round(event.duration_ms)} ms`;
    meta.appendChild(duration);
  }
  return item;
}

async function refreshTrace() {
  if (!state.requestId) return;
  try {
    const payload = await api(`/api/requests/${state.requestId}/trace`);
    elements.activity.replaceChildren(...payload.events.map(activityItem));
    const count = payload.events.length;
    elements.stepCount.textContent = `${count} étape${count > 1 ? "s" : ""}`;
  } catch (error) {
    console.error(error);
  }
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    elements.connection.classList.add("ready");
    elements.connection.lastChild.textContent = " PMS prêt";
    elements.pmsStatus.textContent = `${health.pms} · prêt à recevoir des demandes`;
    elements.mode.textContent = health.decision_mode === "openai_compatible" ? "Mode IA" : "Mode démo local";
    const labels = {
      search_reservations: "Réservations",
      get_reservation: "Détails séjour",
      query_stays: "Séjours présents",
      list_arrivals: "Arrivées",
      list_departures: "Départs",
      get_invoice: "Factures",
      issue_invoice: "Émission de facture",
      list_customer_requests: "Demandes clients",
      get_customer_request: "Fil client",
      reply_customer_request: "Réponse client",
      get_occupancy: "Occupation",
      get_occupancy_range: "Occupation période",
      check_availability: "Disponibilités",
      quote_stay: "Devis",
      create_reservation: "Création réservation",
      update_reservation: "Modification réservation",
      check_in_reservation: "Check-in",
      check_out_reservation: "Check-out",
      add_extra: "Services",
    };
    health.capabilities.forEach((capability) => {
      if (!labels[capability]) return;
      const badge = document.createElement("span");
      badge.textContent = labels[capability];
      elements.capabilities.appendChild(badge);
    });
  } catch (error) {
    elements.connection.classList.add("error");
    elements.connection.lastChild.textContent = " Service indisponible";
    elements.pmsStatus.textContent = "L’application locale ne répond pas";
    console.error(error);
  }
}

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = elements.input.value.trim();
  if (!message || state.busy) return;
  elements.input.value = "";
  sendMessage(message);
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    elements.input.value = button.dataset.prompt;
    elements.input.focus();
  });
});

elements.reset.addEventListener("click", () => {
  state.requestId = null;
  state.awaitingClarification = false;
  state.awaitingConfirmation = false;
  elements.messages.querySelectorAll(".message:not(:first-child)").forEach((node) => node.remove());
  elements.activity.innerHTML = `<li class="empty-activity"><span class="empty-line"></span><p>Les outils utilisés et leurs résultats apparaîtront ici.</p></li>`;
  elements.stepCount.textContent = "0 étape";
  elements.input.focus();
});

loadHealth();
