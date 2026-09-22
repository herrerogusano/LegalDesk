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
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  const controls = ["matter-select", "document-file", "upload-button", "question", "ask-button", "sync-button", "metadata-button", "review-button", "audit-button"];
  const DOCUMENT_STATUS_LABELS = Object.freeze({
    PENDING_UPLOAD: "Pendiente de subida",
    UPLOADED: "Pendiente de indexación",
    PENDING_INGESTION: "Procesando",
    INDEXED: "Listo para consultar",
    FAILED: "Requiere atención",
  });
  const UPLOAD_STAGES = Object.freeze(["authorize", "upload", "verify", "index"]);

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
    const retryable = state.documents.some((item) => item && ["UPLOADED", "FAILED"].includes(item.status));
    $("matter-select").disabled = !enabled;
    $("document-file").disabled = !authorized() || state.busy;
    $("upload-button").disabled = !authorized() || state.busy || !hasFile;
    $("question").disabled = !authorized() || state.busy;
    $("ask-button").disabled = !authorized() || state.busy;
    $("sync-button").disabled = !authorized() || state.busy || !retryable;
    $("sync-button").hidden = !retryable;
    $("sync-helper").hidden = !retryable;
    $("metadata-button").disabled = !authorized() || state.busy;
    $("review-button").disabled = !authorized() || state.busy;
    $("audit-button").disabled = !enabled || state.busy;
    $("login-button").hidden = enabled;
    $("logout-button").hidden = !enabled;
  }

  function setBusy(value) {
    state.busy = value;
    refreshControls();
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
      const message = value && typeof value.error === "string" ? value.error : `La operación no se pudo completar (${response.status}).`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return value;
  }

  function clearMatterView() {
    $("matter-kicker").textContent = "SELECCIONA UN EXPEDIENTE";
    $("document-status").textContent = "Selecciona un expediente para empezar.";
    $("document-count").textContent = "0 documentos";
    $("document-list").replaceChildren();
    $("document-list").append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "Selecciona un expediente para ver sus documentos." }));
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
  }

  function renderDocuments(documents) {
    state.documents = Array.isArray(documents) ? documents : [];
    const count = state.documents.length;
    $("document-count").textContent = `${count} ${count === 1 ? "documento" : "documentos"}`;
    const processing = state.documents.filter((item) => item && ["UPLOADED", "PENDING_INGESTION", "PENDING_UPLOAD"].includes(item.status)).length;
    const failed = state.documents.filter((item) => item && item.status === "FAILED").length;
    const indexed = state.documents.filter((item) => item && item.status === "INDEXED").length;
    if (!count) $("document-status").textContent = "No hay documentos autorizados en este expediente.";
    else if (failed) $("document-status").textContent = `${failed} ${failed === 1 ? "documento requiere" : "documentos requieren"} atención.`;
    else if (processing) $("document-status").textContent = `${processing} ${processing === 1 ? "documento se está" : "documentos se están"} preparando para consulta.`;
    else if (indexed) $("document-status").textContent = `${indexed} ${indexed === 1 ? "documento está" : "documentos están"} listos para consultar.`;
    else $("document-status").textContent = "Revisa el estado de los documentos del expediente.";
    const list = $("document-list");
    list.replaceChildren();
    if (!state.documents.length) {
      list.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "Aún no hay documentos autorizados." }));
      refreshControls();
      return;
    }
    state.documents.forEach((documentRecord) => {
      const item = document.createElement("li");
      item.className = "document-item";
      const name = document.createElement("strong");
      name.textContent = typeof documentRecord.name === "string" ? documentRecord.name : "Documento";
      const details = document.createElement("span");
      details.textContent = DOCUMENT_STATUS_LABELS[documentRecord.status] || "Estado no disponible";
      item.dataset.status = documentRecord.status || "unknown";
      item.append(name, details);
      list.append(item);
    });
    refreshControls();
  }

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
      await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents/${encodeURIComponent(documentId)}/confirm`, { method: "POST", body: "{}", signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      setUploadProgress("index", "Indexando documento…");
      await api(`/api/matters/${encodeURIComponent(state.matterId)}/sync`, { method: "POST", body: JSON.stringify({ documentIds: [documentId] }), signal: state.controller.signal });
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
    if (window.LegalDeskCitationPanel && window.LegalDeskCitationPanel.renderOperationalState) window.LegalDeskCitationPanel.renderOperationalState("documents_processing");
    setBusy(true);
    try {
      setMessage("Consultando los documentos autorizados…", false);
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
        setMessage("Consulta completada.", false);
      }
      await loadHistory(generation);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent(generation)) return;
      window.LegalDeskCitationPanel.renderOperationalState("error");
      setMessage(error.message, true);
    } finally {
      if (isCurrent(generation)) setBusy(false);
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
    setBusy(true);
    try {
      const result = await api(`/api/matters/${encodeURIComponent(state.matterId)}/review`, { method: "POST", body: JSON.stringify({ conversationId: state.conversationId, sessionId: state.sessionId, reasonCode: "user_requested_review", originCorrelationId: state.correlationId, idempotencyKey: `${state.conversationId}:review` }), signal: state.controller.signal });
      if (isCurrent(generation)) {
        setOperator(result);
        setMessage("Solicitud de revisión registrada.", false);
      }
    } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally { if (isCurrent(generation)) setBusy(false); }
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
    $("metadata-button").addEventListener("click", metadata);
    $("review-button").addEventListener("click", requestReview);
    $("audit-button").addEventListener("click", audit);
    $("sync-button").addEventListener("click", async () => {
      if (!authorized() || state.busy || !state.documents.length) return;
      const generation = currentGeneration();
      const documentIds = state.documents.filter((item) => item && ["UPLOADED", "FAILED"].includes(item.status)).map((item) => item.documentId).filter(Boolean);
      if (!documentIds.length) return;
      setBusy(true);
      try {
        setMessage("Solicitando sincronización…", false);
        await api(`/api/matters/${encodeURIComponent(state.matterId)}/sync`, { method: "POST", body: JSON.stringify({ documentIds }), signal: state.controller.signal });
        if (!isCurrent(generation)) return;
        setMessage("Sincronización solicitada; el estado se actualizará al consultar.", false);
        await loadDocuments(generation);
      } catch (error) { if (error.name !== "AbortError" && isCurrent(generation)) setMessage(error.message, true); } finally { if (isCurrent(generation)) setBusy(false); }
    });
    refreshControls();
    loadMe().catch((error) => setMessage(error.message, true));
  });
})();
