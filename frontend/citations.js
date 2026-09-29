(function () {
  "use strict";

  const statusLabels = {
    answerable: "EVIDENCIA SUFICIENTE",
    ambiguous: "EVIDENCIA AMBIGUA",
    insufficient_evidence: "SIN EVIDENCIA SUFICIENTE",
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
    message.append(document.createTextNode("Buscando en los documentos autorizados…"));
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

  function renderLoadingState() {
    const answer = document.getElementById("answer");
    const status = document.getElementById("evidence-status");
    const loading = ensureLoadingState(answer);
    loading.hidden = false;
    status.dataset.status = "loading";
    status.querySelector("span:last-child").textContent = "BUSCANDO RESPUESTA";
    answer.setAttribute("aria-busy", "true");
  }

  function clearLoadingState(answer) {
    const loading = answer.querySelector(".answer-loading");
    if (loading) loading.hidden = true;
    answer.setAttribute("aria-busy", "false");
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
    const citationNumbers = new Map();
    clearLoadingState(answer);
    document.getElementById("citation-inspection").hidden = true;
    document.getElementById("inspection-meta").textContent = "";
    document.getElementById("inspection-passage").textContent = "";

    list.replaceChildren();
    answer.replaceChildren();
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

  function renderOperationalState(operationStatus) {
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
    answer.append(element("p", "", labels[operationStatus] || "La respuesta aparecerá aquí después de una consulta autorizada."));
  }

  window.LegalDeskCitationPanel = Object.freeze({ renderChatResponse, renderOperationalState, renderLoadingState });
})();
