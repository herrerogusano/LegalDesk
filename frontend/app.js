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

  function setMessage(message, error) {
    const node = $(error ? "app-error" : "app-status");
    const other = $(error ? "app-status" : "app-error");
    node.textContent = message || "";
    node.hidden = !message;
    if (message) other.hidden = true;
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
    $("matter-select").disabled = !enabled;
    $("document-file").disabled = !authorized() || state.busy;
    $("upload-button").disabled = !authorized() || state.busy;
    $("question").disabled = !authorized() || state.busy;
    $("ask-button").disabled = !authorized() || state.busy;
    $("sync-button").disabled = !authorized() || state.busy || !state.documents.some((item) => item && ["UPLOADED", "FAILED"].includes(item.status));
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
    $("evidence-status").dataset.status = "";
    $("evidence-status").querySelector("span:last-child").textContent = "ESPERANDO CONSULTA";
    $("question").value = "";
    $("question-count").textContent = "0 / 1000";
  }

  function renderDocuments(documents) {
    state.documents = Array.isArray(documents) ? documents : [];
    if (state.documents.some((item) => item && ["UPLOADED", "PENDING_INGESTION", "PENDING_UPLOAD"].includes(item.status))) {
      $("document-status").textContent = "Documento recibido; todavía se está procesando.";
    } else if (state.documents.some((item) => item && item.status === "FAILED")) {
      $("document-status").textContent = "Un documento requiere revisar su fallo de indexación.";
    } else if (state.documents.some((item) => item && item.status === "INDEXED")) {
      $("document-status").textContent = "Documento indexado y disponible para consulta.";
    }
    const list = $("document-list");
    list.replaceChildren();
    if (!state.documents.length) {
      list.append(Object.assign(document.createElement("li"), { className: "microcopy", textContent: "No hay documentos autorizados." }));
      return;
    }
    state.documents.forEach((documentRecord) => {
      const item = document.createElement("li");
      item.className = "document-item";
      const name = document.createElement("strong");
      name.textContent = typeof documentRecord.name === "string" ? documentRecord.name : "Documento";
      const details = document.createElement("span");
      details.textContent = `${documentRecord.status || "estado desconocido"} · ${documentRecord.documentId || ""}`;
      item.append(name, details);
      list.append(item);
    });
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
      setMessage("Autorizando subida…", false);
      const authorization = await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents/upload-authorizations`, {
        method: "POST",
        body: JSON.stringify({ filename: file.name, mediaType: file.type || "application/octet-stream", fileSizeBytes: file.size }),
        signal: state.controller.signal,
      });
      if (!isCurrent(generation)) return;
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
      await api(`/api/matters/${encodeURIComponent(state.matterId)}/documents/${encodeURIComponent(documentId)}/confirm`, { method: "POST", body: "{}", signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      await api(`/api/matters/${encodeURIComponent(state.matterId)}/sync`, { method: "POST", body: JSON.stringify({ documentIds: [documentId] }), signal: state.controller.signal });
      if (!isCurrent(generation)) return;
      $("document-status").textContent = "Sincronización solicitada; comprobando estado…";
      await loadDocuments(generation);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrent(generation)) return;
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
      $("inspection-meta").textContent = `${result.documentId || "Documento"} · expediente ${result.matterId || ""}`;
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
