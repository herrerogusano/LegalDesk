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

  function renderChatResponse(response) {
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

    list.replaceChildren();
    answer.replaceChildren();
    citations.forEach((citation, index) => {
      if (!citation || typeof citation.citationId !== "string") return;
      citationNumbers.set(citation.citationId, index + 1);
      const item = element("li", "citation-card");
      item.id = `source-${index + 1}`;
      item.tabIndex = -1;
      item.dataset.citationId = citation.citationId;
      const title = citation.documentName || citation.documentId || "Documento sin nombre";
      item.append(element("p", "citation-title", title));
      const details = [citation.documentId];
      if (citation.pageNumber) details.push(`pág. ${citation.pageNumber}`);
      if (citation.section) details.push(citation.section);
      item.append(element("p", "citation-meta", details.join(" · ")));
      if (citation.sourceUri) item.append(element("p", "citation-source", citation.sourceUri));
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

  window.LegalDeskCitationPanel = Object.freeze({ renderChatResponse });

  const demoResponse = {
    answer: "El acuerdo establece un plazo de 17 días. El anexo precisa que el cómputo comienza al recibirse la factura.",
    citations: [
      {
        citationId: "citation-1",
        documentId: "doc-sundial-agreement",
        documentName: "Acuerdo de servicios — versión ficticia.pdf",
        sourceUri: "s3://legaldesk-demo/fictional/sundial/agreement.pdf",
        pageNumber: 4,
        section: "Payment terms",
      },
      {
        citationId: "citation-2",
        documentId: "doc-sundial-annex",
        documentName: "Anexo de facturación — versión ficticia.pdf",
        sourceUri: "s3://legaldesk-demo/fictional/sundial/annex.pdf",
        pageNumber: 2,
        section: "Invoice cycle",
      },
    ],
    evidenceStatus: "answerable",
    disclaimerRequired: true,
  };

  document.addEventListener("DOMContentLoaded", () => renderChatResponse(demoResponse));
})();
