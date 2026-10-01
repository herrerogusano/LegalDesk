(function (global) {
  "use strict";

  const MAX_LABEL_LENGTH = 160;
  const MAX_ROWS = 50;

  function safeLabel(value) {
    if (typeof value !== "string" && typeof value !== "number") return "";
    const text = String(value).trim();
    if (!text) return "";
    return text.slice(0, MAX_LABEL_LENGTH);
  }

  function parseContent(value) {
    if (!value || typeof value !== "object" || !Array.isArray(value.content)) return null;
    for (const item of value.content.slice(0, MAX_ROWS)) {
      if (!item || typeof item.text !== "string" || item.text.length > 20_000) continue;
      try {
        const parsed = JSON.parse(item.text);
        if (parsed && typeof parsed === "object") return parsed;
      } catch (_error) {
        // Provider-shaped text is not a diagnostic field until it parses as JSON.
      }
    }
    return null;
  }

  function unwrap(value) {
    const envelope = value && typeof value === "object" ? value : {};
    let payload = envelope;
    if (payload.result && typeof payload.result === "object") payload = payload.result;
    const parsed = parseContent(payload);
    if (parsed) payload = parsed;
    if (payload.result && typeof payload.result === "object" && !payload.documents && !payload.items && !payload.events) {
      const nested = parseContent(payload.result) || payload.result;
      payload = nested;
    }
    return { envelope, payload };
  }

  function sourceRecords(payload) {
    const records = Array.isArray(payload && payload.documents)
      ? payload.documents
      : Array.isArray(payload && payload.items) ? payload.items : [];
    return records;
  }

  function sourceRows(payload) {
    return sourceRecords(payload).slice(0, MAX_ROWS).map((record) => ({
      name: safeLabel(record && (record.name || record.documentName)) || "Documento sin nombre",
      status: safeLabel(record && (record.status || record.state)) || "Estado no disponible",
      mediaType: safeLabel(record && (record.mediaType || record.contentType)),
      sizeBytes: Number.isFinite(record && record.fileSizeBytes) ? record.fileSizeBytes : null,
    }));
  }

  function timelineRecords(payload) {
    const records = Array.isArray(payload && payload.events) ? payload.events : [];
    return records;
  }

  function timelineRows(payload) {
    return timelineRecords(payload).slice(-MAX_ROWS).map((record) => ({
      operation: safeLabel(record && (record.operation || record.event_type)) || "Operación no disponible",
      timestampMs: Number.isFinite(record && record.timestampMs) ? record.timestampMs
        : Number.isFinite(record && record.timestamp_ms) ? record.timestamp_ms : null,
      correlationId: safeLabel(record && (record.correlationId || record.correlation_id)),
    }));
  }

  function toDiagnosticsModel(value) {
    const { envelope, payload } = unwrap(value);
    const documentRecords = sourceRecords(payload);
    const eventRecords = timelineRecords(payload);
    const documents = sourceRows(payload);
    const events = timelineRows(payload);
    const status = safeLabel(envelope.status || payload.status || payload.operationStatus);
    const tool = safeLabel(envelope.tool || payload.tool);
    const correlationId = safeLabel(envelope.correlationId || payload.correlationId || payload.correlation_id);
    return {
      summary: [
        status ? ["Estado", status] : null,
        tool ? ["Herramienta", tool] : null,
        correlationId ? ["Correlación", correlationId] : null,
        documentRecords.length ? ["Documentos", documentRecords.length > MAX_ROWS ? `Mostrando ${MAX_ROWS} de ${documentRecords.length}` : `${documentRecords.length} encontrados`] : null,
        eventRecords.length ? ["Eventos", eventRecords.length > MAX_ROWS ? `Mostrando ${MAX_ROWS} de ${eventRecords.length}` : `${eventRecords.length} registrados`] : null,
      ].filter(Boolean),
      documents,
      events,
    };
  }

  global.LegalDeskDiagnostics = Object.freeze({ toDiagnosticsModel });
}(window));
