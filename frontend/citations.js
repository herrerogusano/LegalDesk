(function () {
  "use strict";

  const statusLabels = {
    answerable: "EVIDENCIA SUFICIENTE",
    ambiguous: "EVIDENCIA AMBIGUA",
    insufficient_evidence: "SIN EVIDENCIA SUFICIENTE",
  };

  const DEFAULT_LOADING_MESSAGE = "Buscando en los documentos autorizados…";
  const DEFAULT_LOADING_STATUS = "BUSCANDO RESPUESTA";
  const blockStatusLabels = {
    data: "DATOS EXTRAÍDOS",
    inventory: "INVENTARIO DE DOCUMENTOS",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function ensureLoadingState(answer) {
    let loading = answer.querySelector(".answer-loading");
    if (loading) return loading;
    loading = element("div", "answer-loading");
    loading.hidden = true;
    const message = element("p", "answer-loading-message");
    const indicator = element("span", "answer-loading-indicator");
    indicator.setAttribute("aria-hidden", "true");
    [0, 1, 2].forEach((index) => {
      const dot = element("span", "answer-loading-dot");
      dot.style.setProperty("--dot-delay", `${index * 0.18}s`);
      indicator.append(dot);
    });
    message.append(indicator);
    message.append(element("span", "answer-loading-text", DEFAULT_LOADING_MESSAGE));
    const skeleton = element("div", "answer-loading-skeleton");
    skeleton.setAttribute("aria-hidden", "true");
    ["92%", "76%", "58%"].forEach((width) => {
      const line = element("span", "answer-loading-skeleton-line");
      line.style.setProperty("--skeleton-width", width);
      skeleton.append(line);
    });
    loading.append(message, skeleton);
    answer.append(loading);
    return loading;
  }

  // ``options`` lets the asynchronous agent chat say "pensando…" instead of the
  // synchronous wording; calling it again just updates the visible message.
  function renderLoadingState(options) {
    const settings = options || {};
    const answer = document.getElementById("answer");
    const status = document.getElementById("evidence-status");
    const loading = ensureLoadingState(answer);
    loading.hidden = false;
    loading.querySelector(".answer-loading-text").textContent = settings.message || DEFAULT_LOADING_MESSAGE;
    status.dataset.status = "loading";
    status.querySelector("span:last-child").textContent = settings.statusLabel || DEFAULT_LOADING_STATUS;
    answer.setAttribute("aria-busy", "true");
  }

  function clearLoadingState(answer) {
    const loading = answer.querySelector(".answer-loading");
    if (loading) loading.hidden = true;
    answer.setAttribute("aria-busy", "false");
  }

  // Numbered, inspectable source cards. Returns citationId -> display number.
  function renderCitationList(citations) {
    const list = document.getElementById("citation-list");
    const citationNumbers = new Map();
    list.replaceChildren();
    citations.forEach((citation, index) => {
      if (!citation || typeof citation.citationId !== "string") return;
      citationNumbers.set(citation.citationId, index + 1);
      const item = element("li", "citation-card");
      item.id = `source-${index + 1}`;
      item.tabIndex = -1;
      item.dataset.citationId = citation.citationId;
      // IDs remain in the response for server-side handles and validation, but
      // are never exposed in the normal citation view.
      const title = typeof citation.documentName === "string" && citation.documentName.trim()
        ? citation.documentName.trim()
        : "Documento del expediente";
      item.append(element("p", "citation-title", title));
      const details = [];
      if (citation.pageNumber) details.push(`pág. ${citation.pageNumber}`);
      if (citation.section) details.push(citation.section);
      item.append(element("p", "citation-meta", details.length ? details.join(" · ") : "Pasaje recuperado del expediente"));
      if (typeof citation.handle === "string" && citation.handle) {
        const inspect = element("button", "citation-inspect", "Inspeccionar pasaje");
        inspect.type = "button";
        inspect.dataset.handle = citation.handle;
        inspect.dataset.documentName = title;
        inspect.setAttribute("aria-label", `Inspeccionar el pasaje de la cita ${index + 1}`);
        item.append(inspect);
      }
      list.append(item);
    });
    return citationNumbers;
  }

  function renderChatResponse(response) {
    if (response && ["error", "blocked", "documents_processing"].includes(response.operationStatus)) {
      renderOperationalState(response.operationStatus);
      return;
    }
    if (!response || typeof response !== "object" || typeof response.answer !== "string") {
      throw new TypeError("response must contain a string answer");
    }
    const citations = Array.isArray(response.citations) ? response.citations : [];
    const answer = document.getElementById("answer");
    const status = document.getElementById("evidence-status");
    const disclaimer = document.getElementById("disclaimer");
    const list = document.getElementById("citation-list");
    const empty = document.getElementById("empty-citations");
    clearLoadingState(answer);
    document.getElementById("citation-inspection").hidden = true;
    document.getElementById("inspection-meta").textContent = "";
    document.getElementById("inspection-passage").textContent = "";

    answer.replaceChildren();
    const citationNumbers = renderCitationList(citations);

    const paragraph = element("p", "");
    const citationMarks = response.citationIdsInAnswer && Array.isArray(response.citationIdsInAnswer)
      ? response.citationIdsInAnswer
      : citations.map((citation) => citation && citation.citationId).filter(Boolean);
    paragraph.append(document.createTextNode(response.answer));
    answer.append(paragraph);
    if (citationMarks.length) {
      const references = element("p", "answer-references");
      references.append(document.createTextNode("Fuentes: "));
      citationMarks.forEach((citationId, index) => {
        const number = citationNumbers.get(citationId);
        if (!number) return;
        const link = element("a", "inline-citation", String(number));
        link.href = `#source-${number}`;
        link.setAttribute("aria-label", `Ir a la cita ${number}`);
        references.append(link);
        if (index < citationMarks.length - 1) references.append(document.createTextNode(" "));
      });
      answer.append(references);
    }

    const evidenceStatus = statusLabels[response.evidenceStatus] ? response.evidenceStatus : "insufficient_evidence";
    status.dataset.status = evidenceStatus;
    status.querySelector("span:last-child").textContent = statusLabels[evidenceStatus];
    disclaimer.hidden = response.disclaimerRequired !== true;
    empty.hidden = citations.length > 0;
    list.hidden = citations.length === 0;
    document.getElementById("sources-heading").textContent = citations.length === 1
      ? "Fuente consultada"
      : "Fuentes consultadas";
  }

  function appendCitationMarks(parent, citationIds, citationNumbers, label) {
    const numbers = (Array.isArray(citationIds) ? citationIds : [])
      .map((citationId) => citationNumbers.get(citationId)).filter(Boolean);
    if (!numbers.length) return false;
    if (label) parent.append(document.createTextNode(label));
    numbers.forEach((number, index) => {
      const link = element("a", "inline-citation", String(number));
      link.href = `#source-${number}`;
      link.setAttribute("aria-label", `Ir a la cita ${number}`);
      parent.append(link);
      if (index < numbers.length - 1) parent.append(document.createTextNode(" "));
    });
    return true;
  }

  function renderTextBlock(block, citationNumbers) {
    const section = element("section", "agent-block agent-block-text");
    section.dataset.blockType = "text";
    section.append(element("p", "agent-block-body", block.text));
    const evidence = statusLabels[block.evidenceStatus] ? block.evidenceStatus : "insufficient_evidence";
    const evidenceNode = element("p", "agent-block-status", statusLabels[evidence]);
    evidenceNode.dataset.status = evidence;
    section.append(evidenceNode);
    const references = element("p", "answer-references");
    if (appendCitationMarks(references, block.citationIds, citationNumbers, "Fuentes: ")) section.append(references);
    // The agentic chat offers no review of its answers (owner decision 2026-11-02).
    return section;
  }

  function renderLineGroups(block, citationNumbers, settings) {
    const section = element("section", `agent-block agent-block-${block.type}`);
    section.dataset.blockType = block.type;
    (Array.isArray(block.groups) ? block.groups : []).forEach((group) => {
      if (!group || typeof group !== "object") return;
      const wrap = element("div", "agent-group");
      if (typeof group.title === "string" && group.title) wrap.append(element("h3", "agent-group-title", group.title));
      const list = element("ul", "agent-lines");
      (Array.isArray(group.lines) ? group.lines : []).forEach((line) => {
        if (!line || typeof line.text !== "string") return;
        const item = element("li", "agent-line");
        if (line.pending === true) item.dataset.pending = "true"; // the template text already says so
        const fieldRef = typeof line.documentId === "string" && line.documentId && typeof line.fieldName === "string" && line.fieldName
          ? { documentId: line.documentId, fieldName: line.fieldName, pending: line.pending === true } : null;
        if (fieldRef) {
          item.dataset.documentId = fieldRef.documentId;
          item.dataset.fieldName = fieldRef.fieldName;
        }
        item.append(element("span", "agent-line-text", line.text));
        const marks = element("span", "agent-line-citations");
        if (appendCitationMarks(marks, line.citationIds, citationNumbers, " ")) item.append(marks);
        // Extension point: where "¿dato incorrecto?" leads is decided outside this
        // renderer. Without a ``fieldAction`` the line shows no link at all.
        const action = fieldRef && typeof settings.fieldAction === "function" ? settings.fieldAction(fieldRef) : null;
        if (action && typeof action.label === "string" && typeof action.onClick === "function") {
          const link = element("button", "text-button agent-field-action", action.label);
          link.type = "button";
          link.dataset.agentAction = "field-action";
          link.addEventListener("click", () => action.onClick(fieldRef));
          item.append(link);
        }
        list.append(item);
      });
      wrap.append(list);
      section.append(wrap);
    });
    (Array.isArray(block.aggregates) ? block.aggregates : []).forEach((aggregate) => {
      if (!aggregate || typeof aggregate.text !== "string") return;
      const paragraph = element("p", "agent-aggregate");
      paragraph.append(element("span", "agent-aggregate-text", aggregate.text));
      const marks = element("span", "agent-line-citations");
      if (appendCitationMarks(marks, aggregate.citationIds, citationNumbers, " ")) paragraph.append(marks);
      section.append(paragraph);
    });
    if (typeof block.text === "string" && block.text) section.append(element("p", "agent-block-body", block.text));
    return section;
  }

  // Renders the result of a finished agent chat job. Every value enters as text
  // (the backend already wrote the sentences of data and inventory blocks).
  function renderAgentResult(result, options) {
    const settings = options || {};
    if (result && ["error", "blocked", "documents_processing"].includes(result.operationStatus)) {
      renderOperationalState(result.operationStatus);
      return;
    }
    if (!result || typeof result !== "object" || !Array.isArray(result.blocks) || !result.blocks.length) {
      throw new TypeError("result must contain blocks");
    }
    const citations = Array.isArray(result.citations) ? result.citations : [];
    const answer = document.getElementById("answer");
    const status = document.getElementById("evidence-status");
    clearLoadingState(answer);
    document.getElementById("citation-inspection").hidden = true;
    document.getElementById("inspection-meta").textContent = "";
    document.getElementById("inspection-passage").textContent = "";
    answer.replaceChildren();
    const citationNumbers = renderCitationList(citations);
    let firstText = null;
    result.blocks.forEach((block) => {
      if (!block || typeof block !== "object") return;
      if (block.type === "text" && typeof block.text === "string") {
        answer.append(renderTextBlock(block, citationNumbers));
        if (firstText === null) firstText = block;
      } else if (block.type === "data" || block.type === "inventory") {
        answer.append(renderLineGroups(block, citationNumbers, settings));
      } else if (["intro", "transition", "notice"].includes(block.type) && typeof block.text === "string") {
        answer.append(element("p", `agent-block-lead agent-block-${block.type}`, block.text));
      }
    });
    const types = new Set(result.blocks.map((block) => block && block.type));
    const headline = firstText
      ? (statusLabels[firstText.evidenceStatus] ? firstText.evidenceStatus : "insufficient_evidence")
      : types.has("data") ? "data" : "inventory";
    status.dataset.status = headline;
    status.querySelector("span:last-child").textContent = statusLabels[headline] || blockStatusLabels[headline];
    document.getElementById("disclaimer").hidden = result.disclaimerRequired !== true;
    document.getElementById("empty-citations").hidden = citations.length > 0;
    document.getElementById("citation-list").hidden = citations.length === 0;
    document.getElementById("sources-heading").textContent = citations.length === 1 ? "Fuente consultada" : "Fuentes consultadas";
  }

  function renderOperationalState(operationStatus, message) {
    document.getElementById("citation-inspection").hidden = true;
    document.getElementById("inspection-meta").textContent = "";
    document.getElementById("inspection-passage").textContent = "";
    const answer = document.getElementById("answer");
    const status = document.getElementById("evidence-status");
    const disclaimer = document.getElementById("disclaimer");
    const list = document.getElementById("citation-list");
    const empty = document.getElementById("empty-citations");
    clearLoadingState(answer);
    answer.replaceChildren();
    list.replaceChildren();
    list.hidden = true;
    empty.hidden = false;
    disclaimer.hidden = true;
    const labels = {
      documents_processing: "DOCUMENTOS EN PROCESAMIENTO",
      error: "ERROR OPERATIVO",
      blocked: "OPERACIÓN BLOQUEADA",
    };
    status.dataset.status = operationStatus || "";
    status.querySelector("span:last-child").textContent = labels[operationStatus] || "ESPERANDO CONSULTA";
    answer.append(element("p", "", message || labels[operationStatus] || "La respuesta aparecerá aquí después de una consulta autorizada."));
  }

  window.LegalDeskCitationPanel = Object.freeze({ renderChatResponse, renderAgentResult, renderOperationalState, renderLoadingState });
})();
