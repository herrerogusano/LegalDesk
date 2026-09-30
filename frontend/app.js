(function () {
  "use strict";

  const state = {
    me: null,
    csrfToken: "",
    matterId: "",
    conversationId: "",
    sessionId: "",
    correlationId: "",
    generation: 0,
    controller: new AbortController(),
    documents: [],
    reviews: [],
    reviewDetails: Object.create(null),
    reviewDetailRequests: Object.create(null),
    reviewUpdates: Object.create(null),
    reviewMove: null,
    hasAcceptedAnswer: false,
    busy: false,
    activeDocumentTab: "available",
  };

  const $ = (id) => document.getElementById(id);
  const controls = ["matter-select", "document-file", "upload-button", "question", "ask-button", "sync-button", "metadata-button", "review-button", "audit-button"];
  const DOCUMENT_STATUS_LABELS = Object.freeze({
    PENDING_UPLOAD: "Carga incompleta",
    UPLOADED: "Pendiente de indexación",
    PENDING_INGESTION: "Procesando",
    INDEXED: "Listo para consultar",
    FAILED: "Requiere atención",
  });
  const UPLOAD_STAGES = Object.freeze(["authorize", "upload", "verify", "scan", "index"]);
  const INGESTION_MAX_POLLS = 20;
  const INGESTION_POLL_DELAY_MS = 1500;
  const DOCUMENT_MAX_POLLS = 10;
  const DOCUMENT_POLL_DELAY_MS = 1000;
  const REVIEW_STATUS_LABELS = Object.freeze({ open: "Pendiente", in_review: "En revisión", closed: "Resuelta" });
  const REVIEW_REASON_LABELS = Object.freeze({
    user_requested_review: "Necesito criterio profesional",
    insufficient_evidence: "Falta evidencia suficiente",
    ambiguous_evidence: "La evidencia es ambigua",
    material_legal_judgment: "Requiere juicio jurídico material",
    safety_escalation: "Escalado de seguridad",
  });
  const detailsMotion = new WeakMap();

  function reducedMotionPreferred() {
    return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function motionTiming(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function motionEasing(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function setMessage(message, error, tone) {
    const node = $(error ? "app-error" : "app-status");
    const other = $(error ? "app-status" : "app-error");
    node.textContent = message || "";
    node.hidden = !message;
    node.dataset.state = message ? (error ? "error" : tone || "info") : "";
    other.dataset.state = "";
    if (message) other.hidden = true;
  }

  function clearMessages() {
    ["app-error", "app-status"].forEach((id) => {
      const node = $(id);
      node.textContent = "";
      node.hidden = true;
      node.dataset.state = "";
    });
  }

  function selectedFile() {
    const input = $("document-file");
    return input && input.files && input.files.length ? input.files[0] : null;
  }

  function formatFileSize(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "tamaño desconocido";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderSelectedFile() {
    const file = selectedFile();
    const node = $("selected-file-status");
    if (!file) {
      node.textContent = "Sin archivo seleccionado.";
      node.dataset.state = "empty";
      return;
    }
    node.textContent = `Archivo seleccionado: ${file.name} · ${formatFileSize(file.size)} · ${file.type || "tipo no indicado"}`;
    node.dataset.state = "selected";
  }

  function setUploadProgress(stage, message) {
    const workspace = $("upload-workspace");
    const progress = $("upload-progress");
    const status = $("upload-progress-message");
    const active = UPLOAD_STAGES.includes(stage);
    progress.hidden = stage === "idle";
    workspace.setAttribute("aria-busy", active ? "true" : "false");
    workspace.dataset.state = stage;
    if (message) status.textContent = message;
    progress.dataset.state = stage;
    progress.querySelectorAll("[data-upload-stage]").forEach((step, index) => {
      const stepStage = step.dataset.uploadStage;
      const currentIndex = UPLOAD_STAGES.indexOf(stage);
      const stepState = stage === "complete" || (currentIndex >= 0 && index < currentIndex)
        ? "complete"
        : stepStage === stage ? "active" : stage === "error" ? "error" : "pending";
      step.dataset.state = stepState;
      if (stepStage === stage) step.setAttribute("aria-current", "step");
      else step.removeAttribute("aria-current");
    });
  }

  function setOperator(value) {
    const output = $("operator-output");
    output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function currentGeneration() { return state.generation; }

  function isCurrent(generation) {
    return generation === state.generation;
  }

  function resetController() {
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    return state.controller.signal;
  }

  function authorized() {
    return Boolean(state.me && state.matterId && state.conversationId && state.sessionId);
  }

  function refreshControls() {
    const enabled = Boolean(state.me);
    const hasFile = Boolean(selectedFile());
    const retryable = state.documents.some((item) => item && ["PENDING_UPLOAD", "UPLOADED"].includes(item.status));
    $("matter-select").disabled = !enabled;
    $("document-file").disabled = !authorized() || state.busy;
    $("upload-button").disabled = !authorized() || state.busy || !hasFile;
    $("question").disabled = !authorized() || state.busy;
    $("ask-button").disabled = !authorized() || state.busy;
    $("sync-button").disabled = !authorized() || state.busy || !retryable;
    $("sync-button").hidden = !retryable;
    $("sync-helper").hidden = !retryable;
    $("metadata-button").disabled = !authorized() || state.busy;
    $("review-button").disabled = !authorized() || state.busy || !state.hasAcceptedAnswer;
    ["review-reason", "review-note", "review-due-at"].forEach((id) => { if ($(id)) $(id).disabled = !authorized() || state.busy || !state.hasAcceptedAnswer; });
    $("audit-button").disabled = !enabled || state.busy;
    $("login-button").hidden = enabled;
    $("logout-button").hidden = !enabled;
  }

  function setBusy(value) {
    state.busy = value;
    refreshControls();
  }

  function setQueryLoadingState(active) {
    const askButton = $("ask-button");
    const answer = $("answer");
    if (askButton) {
      askButton.textContent = active ? "Consultando…" : "Consultar";
      if (active) {
        askButton.disabled = true;
        askButton.setAttribute("aria-busy", "true");
      } else {
        askButton.setAttribute("aria-busy", "false");
        askButton.disabled = !authorized() || state.busy;
      }
    }
    if (answer) answer.setAttribute("aria-busy", active ? "true" : "false");
  }

  async function api(path, options) {
    const request = options || {};
    const headers = new Headers(request.headers || {});
    if (request.body && !(request.body instanceof FormData) && !(request.body instanceof Blob)) {
      headers.set("Content-Type", "application/json");
    }
    if (request.method && !["GET", "HEAD", "OPTIONS"].includes(request.method.toUpperCase()) && state.csrfToken) {
      headers.set("X-CSRF-Token", state.csrfToken);
    }
    const response = await fetch(path, { ...request, headers, credentials: "same-origin" });
    let value = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      try { value = await response.json(); } catch (_error) { value = null; }
    }
    if (!response.ok) {
      const publicErrors = {
        quota_exceeded: "Se ha alcanzado el límite mensual de la beta. Contacta con el administrador para continuar.",
      };
      const code = value && typeof value.error === "string" ? value.error : null;
      const message = (code && publicErrors[code]) || `La operación no se pudo completar (${response.status}).`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return value;
  }

  function clearMatterView() {
    clearReviewMotion();
    $("matter-kicker").textContent = "SELECCIONA UN EXPEDIENTE";
    $("document-status").textContent = "Selecciona un expediente para empezar.";
    $("document-count").textContent = "Sin documentos";
    $("document-list").replaceChildren();
    $("document-list").append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "Selecciona un expediente para ver sus documentos." }));
    const incompleteList = $("document-incomplete-list");
    if (incompleteList) {
      incompleteList.replaceChildren();
      incompleteList.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "Selecciona un expediente para ver sus cargas incompletas." }));
    }
    ["document-tab-available-count", "document-tab-incomplete-count"].forEach((id) => {
      const count = $(id);
      if (count) {
        count.textContent = "0";
        count.setAttribute("aria-label", id.includes("incomplete") ? "0 cargas incompletas" : "0 documentos");
      }
    });
    $("history-list").replaceChildren();
    $("history-list").append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "El historial de la sesión aparecerá aquí." }));
    $("answer").replaceChildren(Object.assign(document.createElement("p"), { textContent: "La respuesta aparecerá aquí después de una consulta autorizada." }));
    $("citation-list").replaceChildren();
    $("citation-list").hidden = true;
    $("empty-citations").hidden = false;
    $("citation-inspection").hidden = true;
    $("operator-output").textContent = "";
    const fileInput = $("document-file");
    fileInput.value = "";
    renderSelectedFile();
    setUploadProgress("idle");
    clearMessages();
    $("evidence-status").dataset.status = "";
    $("evidence-status").querySelector("span:last-child").textContent = "ESPERANDO CONSULTA";
    $("question").value = "";
    $("question-count").textContent = "0 / 1000";
    state.documents = [];
    state.activeDocumentTab = "available";
    setDocumentTab("available");
    state.reviews = [];
    state.reviewDetails = Object.create(null);
    state.reviewDetailRequests = Object.create(null);
    state.reviewUpdates = Object.create(null);
    state.hasAcceptedAnswer = false;
    renderReviews([]);
    setReviewDueDateMinimum();
    resetReviewValidation();
    setQueryLoadingState(false);
  }

  function summarizeDocuments(documents) {
    const records = Array.isArray(documents) ? documents : [];
    const summary = { indexed: 0, processing: 0, incomplete: 0, failed: 0 };
    records.forEach((item) => {
      const classification = documentClassification(item);
      if (classification === "indexed") summary.indexed += 1;
      else if (classification === "processing") summary.processing += 1;
      else if (classification === "incomplete") summary.incomplete += 1;
      else if (classification === "failed") summary.failed += 1;
    });
    return summary;
  }

  function isIncompleteUpload(documentRecord) {
    return Boolean(documentRecord) && (
      documentRecord.status === "PENDING_UPLOAD"
      || (documentRecord.status === "FAILED" && documentRecord.malwareScanStatus === "PENDING")
    );
  }

  function documentClassification(documentRecord) {
    if (!documentRecord) return "unknown";
    if (isIncompleteUpload(documentRecord)) return "incomplete";
    if (documentRecord.status === "INDEXED") return "indexed";
    if (["UPLOADED", "PENDING_INGESTION"].includes(documentRecord.status)) return "processing";
    if (documentRecord.status === "FAILED") return "failed";
    return "unknown";
  }

  function documentStatusLabel(documentRecord) {
    return documentClassification(documentRecord) === "incomplete"
      ? "Carga incompleta"
      : DOCUMENT_STATUS_LABELS[documentRecord && documentRecord.status] || "Estado no disponible";
  }

  // Operational documents stay together except for incomplete uploads. This
  // includes reconciled abandoned uploads represented as FAILED + PENDING scan.
  function partitionDocuments(documents) {
    const records = Array.isArray(documents) ? documents.filter(Boolean) : [];
    return {
      operational: records.filter((item) => documentClassification(item) !== "incomplete"),
      incomplete: records.filter((item) => documentClassification(item) === "incomplete"),
    };
  }

  function setDocumentTab(tabName, focus) {
    const tab = tabName === "incomplete" ? "incomplete" : "available";
    const tabIds = { available: "document-tab-available", incomplete: "document-tab-incomplete" };
    const panelIds = { available: "document-panel-available", incomplete: "document-panel-incomplete" };
    const availableTab = $(tabIds.available);
    const incompleteTab = $(tabIds.incomplete);
    const availablePanel = $(panelIds.available);
    const incompletePanel = $(panelIds.incomplete);
    if (!availableTab || !incompleteTab || !availablePanel || !incompletePanel) return;
    state.activeDocumentTab = tab;
    [["available", availableTab, availablePanel], ["incomplete", incompleteTab, incompletePanel]].forEach(([name, tabNode, panelNode]) => {
      const selected = name === tab;
      tabNode.setAttribute("aria-selected", String(selected));
      tabNode.setAttribute("tabindex", selected ? "0" : "-1");
      panelNode.hidden = !selected;
    });
    if (focus) $(tabIds[tab]).focus();
  }

  function bindDocumentTabs() {
    const tablist = $("document-tabs");
    const tabs = [$("document-tab-available"), $("document-tab-incomplete")];
    if (!tablist || tabs.some((tab) => !tab)) return;
    tabs.forEach((tab) => tab.addEventListener("click", () => setDocumentTab(tab.id === "document-tab-incomplete" ? "incomplete" : "available")));
    tablist.addEventListener("keydown", (event) => {
      const currentIndex = tabs.indexOf(event.target);
      if (currentIndex < 0) return;
      let nextIndex = currentIndex;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % tabs.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = tabs.length - 1;
      else return;
      event.preventDefault();
      const nextTab = tabs[nextIndex];
      setDocumentTab(nextTab.id === "document-tab-incomplete" ? "incomplete" : "available", true);
    });
    setDocumentTab(state.activeDocumentTab);
  }

  function documentSummaryLabel(summary) {
    const parts = [];
    if (summary.indexed) parts.push(`${summary.indexed} ${summary.indexed === 1 ? "listo" : "listos"}`);
    if (summary.processing) parts.push(`${summary.processing} en procesamiento`);
    if (summary.incomplete) parts.push(`${summary.incomplete} ${summary.incomplete === 1 ? "carga incompleta" : "cargas incompletas"}`);
    if (summary.failed) parts.push(`${summary.failed} ${summary.failed === 1 ? "fallo" : "fallos"}`);
    return parts.length ? parts.join(" · ") : "Sin documentos";
  }

  function formatDocumentDateTime(value) {
    if (typeof value !== "string" || !value) return "fecha de autorización no disponible";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "fecha de autorización no disponible";
    return `autorizado ${new Intl.DateTimeFormat("es-ES", { dateStyle: "short", timeStyle: "short" }).format(parsed)}`;
  }

  function documentViewModel(documentRecord) {
    const documentId = typeof documentRecord.documentId === "string" ? documentRecord.documentId : "";
    const shortId = documentId ? `ID ${documentId.slice(0, 8)}` : "ID no disponible";
    return {
      name: typeof documentRecord.name === "string" ? documentRecord.name : "Documento",
      statusLabel: documentStatusLabel(documentRecord),
      metadata: `${shortId} · ${formatFileSize(typeof documentRecord.fileSizeBytes === "number" ? documentRecord.fileSizeBytes : Number.NaN)} · ${formatDocumentDateTime(documentRecord.uploadedAt)}`,
    };
  }

  function renderDocumentList(list, records, emptyText) {
    if (!list) return;
    list.replaceChildren();
    if (!records.length) {
      list.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: emptyText }));
      return;
    }
    records.forEach((documentRecord) => {
      const item = document.createElement("li");
      item.className = "document-item";
      const view = documentViewModel(documentRecord);
      const name = document.createElement("strong");
      name.textContent = view.name;
      const status = document.createElement("span");
      status.className = "document-status";
      status.textContent = view.statusLabel;
      const metadata = document.createElement("span");
      metadata.className = "document-meta";
      metadata.textContent = view.metadata;
      item.dataset.status = documentRecord.status || "unknown";
      item.append(name, status, metadata);
      list.append(item);
    });
  }

  function renderDocuments(documents) {
    state.documents = Array.isArray(documents) ? documents : [];
    const summary = summarizeDocuments(state.documents);
    const groups = partitionDocuments(state.documents);
    const summaryLabel = documentSummaryLabel(summary);
    $("document-count").textContent = summaryLabel;
    const availableCount = $("document-tab-available-count");
    const incompleteCount = $("document-tab-incomplete-count");
    if (availableCount) {
      availableCount.textContent = String(groups.operational.length);
      availableCount.setAttribute("aria-label", `${groups.operational.length} ${groups.operational.length === 1 ? "documento" : "documentos"}`);
    }
    if (incompleteCount) {
      incompleteCount.textContent = String(groups.incomplete.length);
      incompleteCount.setAttribute("aria-label", `${groups.incomplete.length} ${groups.incomplete.length === 1 ? "carga incompleta" : "cargas incompletas"}`);
    }
    if (!state.documents.length) $("document-status").textContent = "No hay documentos autorizados en este expediente.";
    else {
      const details = [];
      if (summary.indexed) details.push(`${summary.indexed} ${summary.indexed === 1 ? "está listo" : "están listos"} para consultar`);
      if (summary.processing) details.push(`${summary.processing} ${summary.processing === 1 ? "sigue" : "siguen"} en procesamiento`);
      if (summary.incomplete) details.push(`${summary.incomplete} ${summary.incomplete === 1 ? "tiene una carga incompleta" : "tienen cargas incompletas"}`);
      if (summary.failed) details.push(`${summary.failed} ${summary.failed === 1 ? "requiere" : "requieren"} atención`);
      $("document-status").textContent = `${details.join(" · ")}.`;
    }
    renderDocumentList($("document-list"), groups.operational, "Aún no hay documentos operativos en este expediente.");
    renderDocumentList($("document-incomplete-list"), groups.incomplete, "No hay cargas incompletas en este expediente.");
    refreshControls();
  }

  window.LegalDeskDocumentView = Object.freeze({
    documentSummaryLabel,
    documentViewModel,
    documentClassification,
    isIncompleteUpload,
    bindDocumentTabs,
    partitionDocuments,
    renderDocuments,
    summarizeDocuments,
  });

  function renderHistory(events) {
    const list = $("history-list");
    list.replaceChildren();
    if (!Array.isArray(events) || !events.length) {
      list.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "No hay historial aceptado en esta sesión." }));
      return;
    }
    events.slice(-100).forEach((event) => {
      if (!event || typeof event.text !== "string") return;
      const item = document.createElement("li");
      item.className = "history-item";
      const role = document.createElement("strong");
      role.textContent = event.role === "ASSISTANT" ? "Respuesta: " : "Pregunta: ";
      const text = document.createElement("span");
      text.textContent = event.text;
      item.append(role, text);
      list.append(item);
    });
  }

  function dateOnly(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = new Date(`${value}T00:00:00`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatReviewDate(value) {
    const parsed = dateOnly(value) || (typeof value === "string" ? new Date(value) : null);
    if (!parsed || Number.isNaN(parsed.getTime())) return "fecha no disponible";
    return new Intl.DateTimeFormat("es-ES", { dateStyle: "long" }).format(parsed);
  }

  function reviewDueState(value, status) {
    if (status === "closed") return `Resuelta · fecha objetivo ${formatReviewDate(value)}`;
    const due = dateOnly(value);
    if (!due) return "Fecha objetivo no disponible";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((due.getTime() - today.getTime()) / 86400000);
    if (days < 0) return `Atrasada · fecha objetivo ${formatReviewDate(value)}`;
    if (days === 0) return `Vence hoy · ${formatReviewDate(value)}`;
    if (days < 2) return `Próxima · ${formatReviewDate(value)}`;
    return `Fecha objetivo · ${formatReviewDate(value)}`;
  }

  function reviewUrgency(value, status) {
    if (status === "closed") return "resolved";
    const due = dateOnly(value);
    if (!due) return "on-track";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((due.getTime() - today.getTime()) / 86400000);
    if (days < 0) return "overdue";
    if (days === 0) return "today";
    if (days < 2) return "soon";
    return "on-track";
  }

  function appendReviewField(parent, label, value) {
    const paragraph = document.createElement("p");
    const labelNode = document.createElement("span");
    labelNode.className = "review-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("span");
    valueNode.textContent = value || "No indicado.";
    paragraph.append(labelNode, valueNode);
    parent.append(paragraph);
  }

  function reviewViewState() {
    const view = {
      open: new Set(),
      focus: null,
      resolutionNotes: Object.create(null),
    };
    document.querySelectorAll("#reviews-section details[data-review-id]").forEach((details) => {
      const reviewId = details.dataset.reviewId;
      if (!reviewId) return;
      if (details.open) view.open.add(reviewId);
      const resolution = details.querySelector("textarea[data-resolution-for]");
      if (resolution) view.resolutionNotes[reviewId] = resolution.value;
      if (document.activeElement && details.contains(document.activeElement)) {
        const active = document.activeElement;
        view.focus = {
          reviewId,
          kind: active.matches("summary") ? "summary" : active.dataset.reviewAction || (active.dataset.resolutionFor ? "resolution" : "body"),
        };
      }
    });
    return view;
  }

  function restoreReviewView(view) {
    if (!view) return;
    view.open.forEach((reviewId) => {
      const details = document.querySelector(`#reviews-section details[data-review-id="${CSS.escape(reviewId)}"]`);
      if (!details) return;
      details.dataset.motionRestore = "true";
      details.open = true;
    });
    Object.entries(view.resolutionNotes).forEach(([reviewId, value]) => {
      const field = document.querySelector(`#reviews-section textarea[data-resolution-for="${CSS.escape(reviewId)}"]`);
      if (field && field.value !== value) field.value = value;
    });
    if (view.focus) {
      const details = document.querySelector(`#reviews-section details[data-review-id="${CSS.escape(view.focus.reviewId)}"]`);
      if (!details) return;
      const target = view.focus.kind === "summary"
        ? details.querySelector("summary")
        : details.querySelector(`[data-review-action="${CSS.escape(view.focus.kind)}"], [data-resolution-for="${CSS.escape(view.focus.reviewId)}"]`) || details.querySelector("summary");
      if (target) {
        try { target.focus({ preventScroll: true }); } catch (_error) { target.focus(); }
      }
    }
  }

  function cleanDetailsAnimation(details) {
    const body = details.querySelector(".review-item-body, .technical-diagnostics-body");
    if (body) body.style.removeProperty("opacity");
    if (body) body.style.removeProperty("transform");
    if (body) body.style.removeProperty("clip-path");
    if (body) body.style.removeProperty("height");
    if (body) body.style.removeProperty("overflow");
  }

  function animateReviewDetails(details, opening) {
    const body = details.querySelector(".review-item-body, .technical-diagnostics-body");
    if (!body || reducedMotionPreferred()) {
      cleanDetailsAnimation(details);
      return Promise.resolve();
    }
    const previous = detailsMotion.get(details);
    const renderedHeight = body.getBoundingClientRect().height;
    if (previous && previous.animation) previous.animation.cancel();
    const naturalHeight = body.getBoundingClientRect().height;
    const startHeight = previous ? renderedHeight : opening ? 0 : naturalHeight;
    body.style.overflow = "hidden";
    const animation = body.animate(
      opening
        ? [
          { height: `${startHeight}px`, opacity: 0, transform: "translateY(-6px)", clipPath: "inset(0 0 8% 0)" },
          { height: `${naturalHeight}px`, opacity: 1, transform: "translateY(0)", clipPath: "inset(0 0 0 0)" },
        ]
        : [
          { height: `${startHeight}px`, opacity: 1, transform: "translateY(0)", clipPath: "inset(0 0 0 0)" },
          { height: "0px", opacity: 0, transform: "translateY(-4px)", clipPath: "inset(0 0 8% 0)" },
        ],
      { duration: motionTiming("--motion-duration-details", 240), easing: motionEasing("--motion-ease-emphasized", "cubic-bezier(.22, 1, .36, 1)"), fill: "both" },
    );
    const current = { animation, closing: !opening };
    detailsMotion.set(details, current);
    return animation.finished.catch(() => {}).then(() => {
      if (detailsMotion.get(details) === current && !current.closing) {
        detailsMotion.delete(details);
        current.animation.cancel();
        cleanDetailsAnimation(details);
      }
    });
  }

  function animateDetailsClose(details) {
    if (reducedMotionPreferred() || !details.querySelector(".review-item-body, .technical-diagnostics-body") || typeof details.querySelector("summary")?.animate !== "function") {
      details.open = false;
      return;
    }
    const current = detailsMotion.get(details);
    if (current && current.closing) {
      current.closing = false;
      animateReviewDetails(details, true);
      return;
    }
    const closingAnimation = animateReviewDetails(details, false);
    const closing = detailsMotion.get(details);
    if (closing) closing.closing = true;
    closingAnimation.then(() => {
      if (!closing || !details.isConnected || detailsMotion.get(details) !== closing || !closing.closing) return;
      details.dataset.motionCommit = "true";
      details.open = false;
      delete details.dataset.motionCommit;
      detailsMotion.delete(details);
      closing.animation.cancel();
      cleanDetailsAnimation(details);
    });
  }

  function bindAnimatedDetails(details) {
    const summary = details.querySelector("summary");
    if (!summary || summary.dataset.motionBound === "true") return;
    summary.dataset.motionBound = "true";
    summary.addEventListener("click", (event) => {
      if (!details.open) return;
      event.preventDefault();
      animateDetailsClose(details);
    });
    details.addEventListener("toggle", () => {
      if (!details.open) {
        if (!details.dataset.motionCommit) cleanDetailsAnimation(details);
        return;
      }
      if (details.dataset.motionRestore) {
        delete details.dataset.motionRestore;
        return;
      }
      if (details.dataset.motionCommit) return;
      animateReviewDetails(details, true);
      const reviewId = details.dataset.reviewId;
      if (reviewId && !state.reviewDetails[reviewId]) loadReviewDetail(reviewId, details);
    });
  }

  function captureReviewMove(reviewTaskId) {
    if (reducedMotionPreferred()) return null;
    const item = Array.from(document.querySelectorAll("#reviews-section .review-item")).find((candidate) => candidate.querySelector(`details[data-review-id="${CSS.escape(reviewTaskId)}"]`));
    if (!item) return null;
    const rect = item.getBoundingClientRect();
    const clone = item.cloneNode(true);
    clone.classList.add("review-item--motion-clone");
    clone.setAttribute("aria-hidden", "true");
    clone.setAttribute("inert", "");
    clone.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
    clone.style.left = `${rect.left}px`;
    clone.style.top = `${rect.top}px`;
    clone.style.width = `${rect.width}px`;
    clone.style.transformOrigin = "top left";
    document.body.append(clone);
    return { reviewTaskId, rect, clone };
  }

  function isInViewport(rect) {
    return rect.top >= 0 && rect.bottom <= window.innerHeight && rect.left >= 0 && rect.right <= window.innerWidth;
  }

  function announceResolvedReview(reviewTaskId) {
    const status = $("app-status");
    if (!status) return;
    status.replaceChildren(document.createTextNode("Revisión cerrada y conservada en el historial del expediente. "));
    const link = document.createElement("a");
    link.href = "#reviews-resolved";
    link.className = "review-jump-link";
    link.dataset.reviewJump = reviewTaskId;
    link.textContent = "Ver revisión resuelta";
    status.append(link);
    status.hidden = false;
    status.dataset.state = "success";
    $("app-error").hidden = true;
  }

  function animateReviewMove(reviewTaskId) {
    const move = state.reviewMove;
    if (!move || move.reviewTaskId !== reviewTaskId) return;
    state.reviewMove = null;
    const destination = Array.from(document.querySelectorAll("#reviews-resolved details[data-review-id]")).find((details) => details.dataset.reviewId === reviewTaskId);
    const destinationItem = destination && destination.closest(".review-item");
    const clone = move.clone;
    if (!destinationItem || !clone || !clone.isConnected) {
      if (clone && clone.isConnected) clone.remove();
      return;
    }
    const destinationRect = destinationItem.getBoundingClientRect();
    if (!isInViewport(destinationRect)) {
      clone.remove();
      announceResolvedReview(reviewTaskId);
      return;
    }
    destinationItem.style.opacity = "0";
    const dx = destinationRect.left - move.rect.left;
    const dy = destinationRect.top - move.rect.top;
    const scaleX = destinationRect.width / move.rect.width;
    const scaleY = destinationRect.height / move.rect.height;
    const animation = clone.animate(
      [{ transform: "translate(0, 0) scale(1, 1)", opacity: 1 }, { transform: `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})`, opacity: .15 }],
      { duration: motionTiming("--motion-duration-review-travel", 420), easing: motionEasing("--motion-ease-emphasized", "cubic-bezier(.22, 1, .36, 1)") },
    );
    animation.finished.catch(() => {}).then(() => {
      clone.remove();
      destinationItem.style.removeProperty("opacity");
      destination.animate([{ opacity: 0, transform: "translateY(5px)" }, { opacity: 1, transform: "translateY(0)" }], {
        duration: motionTiming("--motion-duration-details", 240),
        easing: motionEasing("--motion-ease-emphasized", "cubic-bezier(.22, 1, .36, 1)"),
      });
    });
  }

  function clearReviewMotion() {
    if (state.reviewMove && state.reviewMove.clone && state.reviewMove.clone.isConnected) state.reviewMove.clone.remove();
    state.reviewMove = null;
    document.querySelectorAll("#reviews-section details").forEach((details) => {
      const current = detailsMotion.get(details);
      if (current && current.animation) current.animation.cancel();
      detailsMotion.delete(details);
      cleanDetailsAnimation(details);
    });
    document.querySelectorAll(".review-item--motion-clone").forEach((clone) => clone.remove());
  }

  function renderReviewItem(task) {
    const item = document.createElement("li");
    item.className = "review-item";
    item.dataset.urgency = reviewUrgency(task.dueAt, task.status);
    const details = document.createElement("details");
    details.dataset.reviewId = typeof task.reviewTaskId === "string" ? task.reviewTaskId : "";
    if (task.reviewTaskId && state.reviewUpdates[task.reviewTaskId]) details.setAttribute("aria-busy", "true");
    const summary = document.createElement("summary");
    const summaryText = document.createElement("span");
    const title = document.createElement("span");
    title.className = "review-item-title";
    title.textContent = `${REVIEW_STATUS_LABELS[task.status] || "Estado no disponible"} · ${REVIEW_REASON_LABELS[task.reasonCode] || "Motivo registrado"}`;
    const meta = document.createElement("span");
    meta.className = "review-item-meta";
    meta.textContent = `${reviewDueState(task.dueAt, task.status)} · creada ${formatReviewDate(task.createdAt)}`;
    summaryText.append(title, meta);
    summary.append(summaryText);
    details.append(summary);

    const body = document.createElement("div");
    body.className = "review-item-body";
    const snapshot = task && task.snapshot && typeof task.snapshot === "object" ? task.snapshot : null;
    if (!snapshot) {
      const stateMessage = document.createElement("p");
      stateMessage.className = "review-detail-state";
      stateMessage.setAttribute("role", "status");
      stateMessage.textContent = "Abre esta revisión para cargar la respuesta y sus fuentes autorizadas.";
      body.append(stateMessage);
    } else {
      appendReviewField(body, "Pregunta", snapshot.question);
      appendReviewField(body, "Respuesta guardada", snapshot.answer);
    }
    const citations = snapshot && Array.isArray(snapshot.citations) ? snapshot.citations : [];
    if (snapshot && citations.length) {
      const citationWrap = document.createElement("div");
      const citationLabel = document.createElement("span");
      citationLabel.className = "review-label";
      citationLabel.textContent = "Fuentes usadas";
      citationWrap.append(citationLabel);
      const citationList = document.createElement("ul");
      citationList.className = "review-citation";
      citations.forEach((citation) => {
        if (!citation || typeof citation !== "object") return;
        const citationItem = document.createElement("li");
        const source = citation.documentName || "Documento del expediente";
        const location = citation.pageNumber != null ? ` · página ${citation.pageNumber}` : citation.section ? ` · ${citation.section}` : "";
        citationItem.textContent = `${source}${location}${citation.passage ? `: ${citation.passage}` : ""}`;
        citationList.append(citationItem);
      });
      citationWrap.append(citationList);
      body.append(citationWrap);
    }
    if (snapshot) {
      appendReviewField(body, "Nota de solicitud", task.note);
      if (task.status === "closed") appendReviewField(body, "Nota de resolución", task.resolutionNote);
    }

    if (task.status !== "closed" && typeof task.reviewTaskId === "string") {
      const updating = Boolean(state.reviewUpdates[task.reviewTaskId]);
      const actions = document.createElement("div");
      actions.className = "review-actions";
      const inReview = document.createElement("button");
      inReview.type = "button";
      inReview.className = "button review-action-button";
      inReview.dataset.reviewAction = "in-review";
      inReview.dataset.reviewId = task.reviewTaskId;
      inReview.textContent = updating ? "Actualizando…" : "Marcar en revisión";
      inReview.disabled = updating || task.status === "in_review";
      actions.append(inReview);
      const resolution = document.createElement("div");
      resolution.className = "review-resolution";
      const resolutionLabel = document.createElement("label");
      resolutionLabel.className = "review-label";
      resolutionLabel.htmlFor = `resolution-${task.reviewTaskId}`;
      resolutionLabel.textContent = "Nota de resolución (obligatoria al cerrar)";
      const resolutionInput = document.createElement("textarea");
      resolutionInput.id = `resolution-${task.reviewTaskId}`;
      resolutionInput.maxLength = 2000;
      resolutionInput.rows = 2;
      resolutionInput.placeholder = "Describe brevemente el criterio o resultado.";
      resolutionInput.dataset.resolutionFor = task.reviewTaskId;
      const close = document.createElement("button");
      close.type = "button";
      close.className = "button button-primary review-action-button";
      close.dataset.reviewAction = "close";
      close.dataset.reviewId = task.reviewTaskId;
      close.textContent = updating ? "Actualizando…" : "Cerrar revisión";
      close.disabled = updating;
      resolution.append(resolutionLabel, resolutionInput);
      actions.append(resolution, close);
      body.append(actions);
    }
    details.append(body);
    bindAnimatedDetails(details);
    item.append(details);
    return item;
  }

  async function loadReviewDetail(reviewTaskId, details) {
    if (state.reviewDetails[reviewTaskId]) return;
    if (state.reviewDetailRequests[reviewTaskId]) return state.reviewDetailRequests[reviewTaskId];
    const body = details.querySelector(".review-item-body");
    const loading = Object.assign(document.createElement("p"), { className: "review-detail-state", textContent: "Cargando detalle de la revisión…" });
    loading.setAttribute("role", "status");
    body.replaceChildren(loading);
    const generation = currentGeneration();
    const request = (async () => {
      try {
      const query = `conversationId=${encodeURIComponent(state.conversationId)}&sessionId=${encodeURIComponent(state.sessionId)}`;
      const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/reviews/${encodeURIComponent(reviewTaskId)}?${query}`, { signal: state.controller.signal });
      if (!isCurrent(generation) || !result || !result.snapshot) throw new Error("El detalle de la revisión no está disponible.");
      state.reviewDetails[reviewTaskId] = result;
      state.reviews = state.reviews.map((task) => task.reviewTaskId === reviewTaskId ? { ...task, ...result } : task);
      renderReviews(state.reviews);
      } catch (error) {
        if (error.name !== "AbortError" && isCurrent(generation) && details.isConnected) {
          const errorNode = Object.assign(document.createElement("p"), { className: "review-detail-state review-detail-error", textContent: `${error.message} Puedes cerrar y volver a abrir para reintentarlo.` });
          errorNode.setAttribute("role", "alert");
          body.replaceChildren(errorNode);
        }
      } finally {
        if (state.reviewDetailRequests[reviewTaskId] === request) delete state.reviewDetailRequests[reviewTaskId];
      }
    })();
    state.reviewDetailRequests[reviewTaskId] = request;
    return request;
  }

  function renderReviews(tasks) {
    const view = reviewViewState();
    state.reviews = (Array.isArray(tasks) ? tasks : []).map((task) => task && task.reviewTaskId && state.reviewDetails[task.reviewTaskId] ? { ...task, ...state.reviewDetails[task.reviewTaskId] } : task);
    const pending = state.reviews.filter((task) => task && task.status !== "closed").sort((left, right) => {
      const urgency = { overdue: 0, today: 1, soon: 2, "on-track": 3 };
      const urgencyDiff = (urgency[reviewUrgency(left.dueAt, left.status)] ?? 4) - (urgency[reviewUrgency(right.dueAt, right.status)] ?? 4);
      if (urgencyDiff) return urgencyDiff;
      return String(left.dueAt || "9999-12-31").localeCompare(String(right.dueAt || "9999-12-31"));
    });
    const resolved = state.reviews.filter((task) => task && task.status === "closed").sort((left, right) => String(right.closedAt || "").localeCompare(String(left.closedAt || "")));
    const overdue = pending.filter((task) => reviewDueState(task.dueAt, task.status).startsWith("Atrasada"));
    $("reviews-summary").textContent = `${pending.length} pendientes${overdue.length ? ` · ${overdue.length} atrasada${overdue.length === 1 ? "" : "s"}` : ""}`;
    [
      ["reviews-pending", pending, "Aún no hay revisiones pendientes."],
      ["reviews-resolved", resolved, "Aún no hay revisiones resueltas."],
    ].forEach(([id, listItems, emptyText]) => {
      const list = $(id);
      list.replaceChildren();
      if (!listItems.length) list.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: emptyText }));
      else listItems.forEach((task) => list.append(renderReviewItem(task)));
    });
    restoreReviewView(view);
    if (state.reviewMove) animateReviewMove(state.reviewMove.reviewTaskId);
    refreshControls();
  }

  function setReviewDueDateMinimum() {
    const input = $("review-due-at");
    if (!input) return;
    input.min = new Date().toISOString().slice(0, 10);
  }

  function setReviewFieldError(fieldId, errorId, message) {
    const field = $(fieldId);
    const error = $(errorId);
    if (!field || !error) return;
    field.setAttribute("aria-invalid", "true");
    error.textContent = message;
    error.hidden = false;
  }

  function clearReviewFieldError(fieldId, errorId) {
    const field = $(fieldId);
    const error = $(errorId);
    if (field) field.setAttribute("aria-invalid", "false");
    if (error) {
      error.textContent = "";
      error.hidden = true;
    }
    const reason = $("review-reason");
    const due = $("review-due-at");
    const status = $("review-form-status");
    if (status && reason && due && reason.getAttribute("aria-invalid") !== "true" && due.getAttribute("aria-invalid") !== "true") {
      status.textContent = "";
      status.hidden = true;
    }
  }

  function resetReviewValidation() {
    [["review-reason", "review-reason-error"], ["review-due-at", "review-due-at-error"]].forEach(([fieldId, errorId]) => {
      const field = $(fieldId);
      const error = $(errorId);
      if (field) field.setAttribute("aria-invalid", "false");
      if (error) {
        error.textContent = "";
        error.hidden = true;
      }
    });
    const status = $("review-form-status");
    if (status) {
      status.textContent = "";
      status.hidden = true;
    }
  }

  function resetReviewForm() {
    const reason = $("review-reason");
    if (reason) reason.value = "";
    const note = $("review-note");
    if (note) note.value = "";
    const due = $("review-due-at");
    if (due) due.value = "";
    setReviewDueDateMinimum();
    resetReviewValidation();
  }

  function hasValidReviewDueDate(value) {
    const due = dateOnly(value);
    if (!due) return false;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return due.getTime() >= today.getTime();
  }

  function validateReviewForm() {
    resetReviewValidation();
    const reason = $("review-reason");
    const due = $("review-due-at");
    const reasonCode = reason ? reason.value.trim() : "";
    const dueAt = due ? due.value.trim() : "";
    let firstInvalid = null;
    if (!reasonCode) {
      setReviewFieldError("review-reason", "review-reason-error", "Selecciona un motivo.");
      firstInvalid = reason;
    }
    if (!dueAt) {
      setReviewFieldError("review-due-at", "review-due-at-error", "Indica una fecha objetivo.");
      if (!firstInvalid) firstInvalid = due;
    } else if (!hasValidReviewDueDate(dueAt)) {
      setReviewFieldError("review-due-at", "review-due-at-error", "Usa una fecha objetivo de hoy o posterior.");
      if (!firstInvalid) firstInvalid = due;
    }
    if (!firstInvalid) return true;
    const status = $("review-form-status");
    if (status) {
      status.textContent = "Revisa los campos marcados antes de guardar la revisión.";
      status.hidden = false;
    }
    setMessage("Revisa los campos marcados antes de guardar la revisión.", true);
    try { firstInvalid.focus({ preventScroll: true }); } catch (_error) { firstInvalid.focus(); }
    return false;
  }

  function focusNewReview(reviewTaskId) {
    if (typeof reviewTaskId !== "string" || !reviewTaskId) return;
    const details = Array.from(document.querySelectorAll("#reviews-section details[data-review-id]"))
      .find((candidate) => candidate.dataset.reviewId === reviewTaskId);
    const item = details && details.closest(".review-item");
    const summary = details && details.querySelector("summary");
    if (!item || !summary) return;

    item.classList.remove("review-item--new");
    item.classList.add("review-item--new");
    const reducedMotion = Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    item.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
    try { summary.focus({ preventScroll: true }); } catch (_error) { summary.focus(); }
    window.setTimeout(() => item.classList.remove("review-item--new"), reducedMotion ? 900 : 1800);
  }

  async function loadDocuments(generation) {
    const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents`, { signal: state.controller.signal });
    if (!isCurrent(generation)) return;
    renderDocuments(result && result.documents);
  }

  async function loadHistory(generation) {
    const result = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}?sessionId=${encodeURIComponent(state.sessionId)}`, { signal: state.controller.signal });
    if (!isCurrent(generation)) return;
    renderHistory(result && result.events);
  }

  async function loadReviews(generation) {
    const query = `conversationId=${encodeURIComponent(state.conversationId)}&sessionId=${encodeURIComponent(state.sessionId)}`;
    const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/reviews?${query}`, { signal: state.controller.signal });
    if (!isCurrent(generation)) return;
    renderReviews(result && result.tasks);
  }

  async function chooseMatter(matterId) {
    state.generation += 1;
    const generation = currentGeneration();
    resetController();
    state.matterId = "";
    state.conversationId = "";
    state.sessionId = "";
    state.correlationId = "";
    state.busy = false;
    clearMatterView();
    refreshControls();
    if (!matterId) return;
    try {
      setMessage("Abriendo el expediente…", false);
      const result = await api("/api/conversations", { method: "POST", body: JSON.stringify({ matterId: matterId }), signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      state.matterId = matterId;
      state.conversationId = result.conversationId;
      state.sessionId = result.sessionId;
      state.correlationId = result.correlationId || "";
      $("matter-kicker").textContent = `EXPEDIENTE · ${matterId}`;
      refreshControls();
      await loadDocuments(generation);
      await loadHistory(generation);
      await loadReviews(generation);
      if (isCurrent(generation)) setMessage("Expediente listo para consulta.", false);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent(generation)) return;
      setMessage(error.message, true);
      refreshControls();
    }
  }

  async function loadMatters() {
    const result = await api("/api/matters");
    const select = $("matter-select");
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = result && Array.isArray(result.matters) && result.matters.length ? "Selecciona un expediente" : "No hay expedientes autorizados";
    select.append(placeholder);
    (result && Array.isArray(result.matters) ? result.matters : []).forEach((matter) => {
      if (!matter || typeof matter.matterId !== "string") return;
      const option = document.createElement("option");
      option.value = matter.matterId;
      option.textContent = matter.name || matter.matterId;
      select.append(option);
    });
    select.disabled = false;
  }

  async function loadMe() {
    try {
      const me = await api("/api/me");
      state.me = me;
      state.csrfToken = typeof me.csrfToken === "string" ? me.csrfToken : "";
      $("auth-status").lastChild.textContent = ` ${me.userId || me.subject || "Sesión activa"}`;
      refreshControls();
      await loadMatters();
    } catch (error) {
      state.me = null;
      state.csrfToken = "";
      refreshControls();
      if (error.status && error.status !== 401 && error.status !== 403) setMessage(error.message, true);
    }
  }

  function waitForIngestionPoll(signal, delay = INGESTION_POLL_DELAY_MS) {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, delay);
      if (!signal) return;
      if (signal.aborted) {
        window.clearTimeout(timer);
        reject(new DOMException("The operation was aborted.", "AbortError"));
        return;
      }
      signal.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("The operation was aborted.", "AbortError"));
      }, { once: true });
    });
  }

  async function pollDocumentUntilReady(documentId, generation, initialDocument) {
    let documentRecord = initialDocument || null;
    for (let attempt = 0; attempt < DOCUMENT_MAX_POLLS; attempt += 1) {
      if (!isCurrent(generation)) return { document: documentRecord, timedOut: false };
      if (documentRecord && ["UPLOADED", "INDEXED", "FAILED"].includes(documentRecord.status)) {
        return { document: documentRecord, timedOut: false };
      }
      if (attempt > 0) await waitForIngestionPoll(state.controller.signal, DOCUMENT_POLL_DELAY_MS);
      const scanMessage = `Analizando documento… (${attempt + 1}/${DOCUMENT_MAX_POLLS})`;
      setUploadProgress("scan", scanMessage);
      if (attempt === 0) setMessage("Analizando documento…", false);
      const result = await api(
        `/api/matters/${encodeURIComponent(state.matterId)}/documents/${encodeURIComponent(documentId)}`,
        { signal: state.controller.signal },
      );
      if (!isCurrent(generation)) return { document: documentRecord, timedOut: false };
      documentRecord = result && result.document;
    }
    return { document: documentRecord, timedOut: true };
  }

  async function startAndPollIngestion(documentIds, generation) {
    const idempotencyKey = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : `ingestion-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const started = await api(`/api/matters/${encodeURIComponent(state.matterId)}/ingestions`, {
      method: "POST",
      body: JSON.stringify({ documentIds, idempotencyKey }),
      signal: state.controller.signal,
    });
    const operationId = started && started.operationId;
    if (typeof operationId !== "string" || !operationId) throw new Error("La indexación no pudo iniciar.");
    for (let attempt = 0; attempt < INGESTION_MAX_POLLS; attempt += 1) {
      if (!isCurrent(generation)) return null;
      if (attempt > 0) await waitForIngestionPoll(state.controller.signal);
      setUploadProgress("index", `Indexando documento… (${attempt + 1}/${INGESTION_MAX_POLLS})`);
      const status = await api(`/api/matters/${encodeURIComponent(state.matterId)}/ingestions/${encodeURIComponent(operationId)}`, { signal: state.controller.signal });
      if (!isCurrent(generation)) return null;
      if (status && status.operationStatus === "documents_indexed") return status;
      if (status && status.operationStatus === "documents_failed") throw new Error("La indexación falló. Puedes reintentarlo cuando el documento figure como pendiente.");
    }
    throw new Error("La indexación está tardando más de lo esperado. Puedes volver a intentarlo cuando el estado se actualice.");
  }

  async function uploadDocument() {
    if (state.busy) return;
    const file = $("document-file").files[0];
    if (!file || !authorized()) return;
    const generation = currentGeneration();
    setBusy(true);
    try {
      setUploadProgress("authorize", "Autorizando subida…");
      setMessage("Autorizando subida…", false);
      const authorization = await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents/upload-authorizations`, {
        method: "POST",
        body: JSON.stringify({ filename: file.name, mediaType: file.type || "application/octet-stream", fileSizeBytes: file.size }),
        signal: state.controller.signal,
      });
      if (!isCurrent(generation)) return;
      setUploadProgress("upload", "Subiendo documento…");
      const uploadResponse = await fetch(authorization.presignedUrl || authorization.uploadUrl, {
        method: authorization.method || "PUT",
        headers: authorization.headers || {},
        body: file,
        credentials: "same-origin",
        signal: state.controller.signal,
      });
      if (!isCurrent(generation)) return;
      if (!uploadResponse.ok) throw new Error("La subida del documento no se pudo completar.");
      const documentId = authorization.document && authorization.document.documentId;
      setUploadProgress("verify", "Verificando documento…");
      const confirmed = await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents/${encodeURIComponent(documentId)}/confirm`, { method: "POST", body: "{}", signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      const analyzed = await pollDocumentUntilReady(documentId, generation, confirmed && confirmed.document);
      if (!isCurrent(generation)) return;
      const analyzedDocument = analyzed.document;
      if (analyzedDocument && analyzedDocument.status === "FAILED") {
        throw new Error("El documento no superó el análisis de seguridad y no se puede indexar.");
      }
      if (analyzed.timedOut || !analyzedDocument || analyzedDocument.status === "PENDING_UPLOAD") {
        await loadDocuments(generation);
        if (!isCurrent(generation)) return;
        setUploadProgress("complete", "Subida completada; el análisis continúa.");
        setMessage("Subida completada. El documento sigue en análisis; usa «Comprobar estado» para continuar cuando esté listo.", false, "success");
        return;
      }
      if (analyzedDocument.status !== "UPLOADED" && analyzedDocument.status !== "INDEXED") {
        throw new Error("El documento no está listo para indexarse. Puedes volver a comprobar su estado.");
      }
      if (analyzedDocument.status === "UPLOADED") {
        setUploadProgress("index", "Indexando documento…");
        await startAndPollIngestion([documentId], generation);
      }
      if (!isCurrent(generation)) return;
      $("document-status").textContent = "Indexando el documento; comprobando estado…";
      await loadDocuments(generation);
      if (isCurrent(generation)) {
        const documentRecord = state.documents.find((item) => item && item.documentId === documentId);
        const status = documentRecord && typeof documentRecord.status === "string" ? documentRecord.status : "UNKNOWN";
        const humanStatus = DOCUMENT_STATUS_LABELS[status] || "Estado no disponible";
        setUploadProgress("complete", `Documento listo: ${humanStatus}.`);
        setMessage(`Subida completada. Estado de indexación: ${humanStatus}.`, false, "success");
      }
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent(generation)) return;
      setUploadProgress("error", "La subida necesita atención. Puedes intentarlo de nuevo.");
      setMessage(error.message, true);
    } finally {
      if (isCurrent(generation)) setBusy(false);
    }
  }

  async function askQuestion(event) {
    event.preventDefault();
    if (!authorized() || state.busy) return;
    const question = $("question").value.trim();
    if (!question || question.length > 1000) return setMessage("La pregunta debe tener entre 1 y 1000 caracteres.", true);
    const generation = currentGeneration();
    state.hasAcceptedAnswer = false;
    refreshControls();
    if (window.LegalDeskCitationPanel && window.LegalDeskCitationPanel.renderLoadingState) window.LegalDeskCitationPanel.renderLoadingState();
    setBusy(true);
    setQueryLoadingState(true);
    try {
      clearMessages();
      const response = await api("/api/chat", { method: "POST", body: JSON.stringify({ matterId: state.matterId, conversationId: state.conversationId, sessionId: state.sessionId, question }), signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      if (typeof response.correlationId === "string") state.correlationId = response.correlationId;
      if (response && response.operationStatus === "documents_processing") {
        window.LegalDeskCitationPanel.renderOperationalState("documents_processing");
        setMessage("Los documentos todavía se están procesando.", false);
      } else if (response && ["error", "blocked"].includes(response.operationStatus)) {
        window.LegalDeskCitationPanel.renderOperationalState(response.operationStatus);
        setMessage("La consulta terminó con un error operativo; no se ha presentado una respuesta como evidencia.", true);
      } else {
        window.LegalDeskCitationPanel.renderChatResponse(response);
        state.hasAcceptedAnswer = Boolean(response && response.operationStatus === "ok" && response.evidenceStatus && ["answerable", "ambiguous", "insufficient_evidence"].includes(response.evidenceStatus));
        resetReviewForm();
        setMessage("Consulta completada. Puedes guardar esta respuesta para revisión.", false);
      }
      refreshControls();
      await loadHistory(generation);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent(generation)) return;
      window.LegalDeskCitationPanel.renderOperationalState("error");
      setMessage(error.message, true);
    } finally {
      if (isCurrent(generation)) {
        setQueryLoadingState(false);
        setBusy(false);
      }
    }
  }

  async function inspectCitation(event) {
    const button = event.target.closest("button[data-handle]");
    if (!button) return;
    const generation = currentGeneration();
    try {
      const result = await api(`/api/citations?handle=${encodeURIComponent(button.dataset.handle)}`, { signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      // The API response deliberately contains technical IDs for authorization,
      // but the normal source view should remain intelligible to legal users.
      $("inspection-meta").textContent = `${button.dataset.documentName || "Documento del expediente"} · pasaje autorizado`;
      $("inspection-passage").textContent = result.passage || "";
      $("citation-inspection").hidden = false;
    } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); }
  }

  async function metadata() {
    if (!authorized() || state.busy) return;
    const generation = currentGeneration();
    setBusy(true);
    try {
      const body = { matterId: state.matterId, conversationId: state.conversationId, sessionId: state.sessionId, originCorrelationId: state.correlationId, jsonrpc: "2.0", id: `metadata-${Date.now()}`, method: "tools/call", params: { name: "list_matter_documents", arguments: {} } };
      const result = await api("/api/mcp", { method: "POST", body: JSON.stringify(body), signal: state.controller.signal });
      if (isCurrent(generation)) setOperator(result);
    } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally { if (isCurrent(generation)) setBusy(false); }
  }

  async function requestReview() {
    if (!authorized() || state.busy) return;
    const generation = currentGeneration();
    const reviewButton = $("review-button");
    const reviewButtonLabel = reviewButton ? reviewButton.textContent : "Guardar para revisión";
    const reasonCode = $("review-reason").value;
    const dueAt = $("review-due-at").value;
    const note = $("review-note").value.trim();
    if (!validateReviewForm()) return;
    if (reviewButton) {
      reviewButton.textContent = "Guardando…";
      reviewButton.disabled = true;
      reviewButton.setAttribute("aria-busy", "true");
    }
    setBusy(true);
    try {
      const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/reviews`, { method: "POST", body: JSON.stringify({ conversationId: state.conversationId, sessionId: state.sessionId, reasonCode, note, dueAt, originCorrelationId: state.correlationId, idempotencyKey: `${state.conversationId}:${state.correlationId}:review` }), signal: state.controller.signal });
      if (isCurrent(generation)) {
        resetReviewForm();
        await loadReviews(generation);
        if (isCurrent(generation)) {
          setMessage(`Revisión guardada en estado Pendiente. Fecha objetivo: ${formatReviewDate(dueAt)}. No se ha asignado ni notificado automáticamente.`, false, "success");
          focusNewReview(result && result.reviewTaskId);
        }
      }
    } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally {
      if (reviewButton) {
        reviewButton.textContent = reviewButtonLabel;
        reviewButton.removeAttribute("aria-busy");
      }
      if (isCurrent(generation)) setBusy(false);
    }
  }

  async function updateReviewTask(reviewTaskId, status, resolutionNote) {
    if (!authorized() || state.busy || !reviewTaskId) return;
    const generation = currentGeneration();
    state.reviewUpdates[reviewTaskId] = { status };
    renderReviews(state.reviews);
    setBusy(true);
    try {
      const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/reviews/${encodeURIComponent(reviewTaskId)}`, {
        method: "PATCH",
        body: JSON.stringify({ conversationId: state.conversationId, sessionId: state.sessionId, status, resolutionNote: resolutionNote || "", originCorrelationId: state.correlationId }),
        signal: state.controller.signal,
      });
      if (isCurrent(generation)) {
        if (status === "closed") state.reviewMove = captureReviewMove(reviewTaskId);
        if (result && result.reviewTaskId) state.reviewDetails[reviewTaskId] = result;
        delete state.reviewUpdates[reviewTaskId];
        setMessage(status === "closed" ? "Revisión cerrada y conservada en el historial del expediente." : "Revisión marcada En revisión.", false, "success");
        await loadReviews(generation);
        if (status === "closed" && state.reviewMove === null) {
          const destination = document.querySelector(`#reviews-resolved details[data-review-id="${CSS.escape(reviewTaskId)}"]`);
          const destinationItem = destination && destination.closest(".review-item");
          if (destinationItem && !isInViewport(destinationItem.getBoundingClientRect())) announceResolvedReview(reviewTaskId);
          else if (destination) {
            try { destination.querySelector("summary").focus({ preventScroll: true }); } catch (_error) { destination.querySelector("summary").focus(); }
          }
        }
      }
    } catch (error) {
      if (isCurrent(generation)) {
        delete state.reviewUpdates[reviewTaskId];
        renderReviews(state.reviews);
        if (error.name !== "AbortError") setMessage(error.message, true);
      }
    } finally {
      if (isCurrent(generation)) setBusy(false);
    }
  }

  async function audit() {
    const generation = currentGeneration();
    if (state.busy) return;
    setBusy(true);
    try {
      const result = await api("/api/audit", { signal: state.controller.signal });
      if (isCurrent(generation)) setOperator(result);
    } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally { if (isCurrent(generation)) setBusy(false); }
  }

  async function logout() {
    try { await api("/logout", { method: "POST", body: "{}" }); } finally { window.location.assign("/"); }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindDocumentTabs();
    bindAnimatedDetails($("technical-diagnostics"));
    document.addEventListener("click", (event) => {
      const jump = event.target.closest("[data-review-jump]");
      if (!jump) return;
      const reviewId = jump.dataset.reviewJump;
      const details = document.querySelector(`#reviews-resolved details[data-review-id="${CSS.escape(reviewId)}"]`);
      if (!details) return;
      event.preventDefault();
      details.closest(".review-item").scrollIntoView({ behavior: reducedMotionPreferred() ? "auto" : "smooth", block: "center" });
      details.open = true;
      try { details.querySelector("summary").focus({ preventScroll: true }); } catch (_error) { details.querySelector("summary").focus(); }
    });
    $("login-button").addEventListener("click", () => { window.location.assign("/login"); });
    $("logout-button").addEventListener("click", logout);
    $("matter-select").addEventListener("change", (event) => chooseMatter(event.target.value));
    $("document-file").addEventListener("change", () => {
      clearMessages();
      renderSelectedFile();
      setUploadProgress("idle");
      refreshControls();
    });
    $("upload-button").addEventListener("click", uploadDocument);
    $("chat-form").addEventListener("submit", askQuestion);
    $("question").addEventListener("input", () => { $("question-count").textContent = `${$("question").value.length} / 1000`; });
    $("citation-list").addEventListener("click", inspectCitation);
    ["reviews-pending", "reviews-resolved"].forEach((listId) => $(listId).addEventListener("click", (event) => {
      const button = event.target.closest("button[data-review-action]");
      if (!button) return;
      const action = button.dataset.reviewAction;
      const reviewTaskId = button.dataset.reviewId;
      if (action === "in-review") updateReviewTask(reviewTaskId, "in_review");
      if (action === "close") {
        const note = $("resolution-" + reviewTaskId);
        if (!note || !note.value.trim()) return setMessage("Añade una nota útil antes de cerrar la revisión.", true);
        updateReviewTask(reviewTaskId, "closed", note.value.trim());
      }
    }));
    $("metadata-button").addEventListener("click", metadata);
    $("review-reason").addEventListener("change", () => clearReviewFieldError("review-reason", "review-reason-error"));
    $("review-due-at").addEventListener("input", () => clearReviewFieldError("review-due-at", "review-due-at-error"));
    $("review-button").addEventListener("click", requestReview);
    $("audit-button").addEventListener("click", audit);
    $("sync-button").addEventListener("click", async () => {
      if (!authorized() || state.busy || !state.documents.length) return;
      const generation = currentGeneration();
      setBusy(true);
      try {
        setMessage("Comprobando el estado de los documentos…", false);
        await loadDocuments(generation);
        if (!isCurrent(generation)) return;
        const documentIds = state.documents
          .filter((item) => item && item.status === "UPLOADED")
          .map((item) => item.documentId)
          .filter(Boolean);
        if (!documentIds.length) {
          const pending = state.documents.some((item) => item && item.status === "PENDING_UPLOAD");
          const failed = state.documents.some((item) => item && item.status === "FAILED");
          if (failed) setMessage("Hay documentos que requieren atención; no se han reintentado como indexación.", true);
          else if (pending) setMessage("El análisis aún no ha terminado. Puedes volver a comprobar el estado.", false);
          return;
        }
        setUploadProgress("index", "Indexando documentos listos…");
        await startAndPollIngestion(documentIds, generation);
        if (!isCurrent(generation)) return;
        setMessage("Indexación completada para los documentos listos.", false, "success");
        await loadDocuments(generation);
      } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally { if (isCurrent(generation)) setBusy(false); }
    });
    setReviewDueDateMinimum();
    refreshControls();
    loadMe().catch((error) => setMessage(error.message, true));
  });
})();
