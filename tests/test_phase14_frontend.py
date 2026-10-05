from __future__ import annotations

import unittest
from pathlib import Path


class Phase14FrontendIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")
        self.diagnostics = (Path(__file__).parents[1] / "frontend" / "diagnostics.js").read_text(encoding="utf-8")

    def test_public_frontend_uses_start_status_and_bounded_polling(self) -> None:
        self.assertIn("/ingestions`,", self.source)
        self.assertIn("/ingestions/${encodeURIComponent(operationId)}", self.source)
        self.assertIn("const INGESTION_MAX_POLLS = 20", self.source)
        self.assertIn("attempt < INGESTION_MAX_POLLS", self.source)
        self.assertIn("La indexación está tardando más de lo esperado", self.source)
        self.assertNotIn("/sync", self.source)
        self.assertIn('src="/diagnostics.js"', (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("LegalDeskDiagnostics", self.source)

    def test_document_analysis_is_bounded_and_ingestion_requires_uploaded(self) -> None:
        self.assertIn('const DOCUMENT_MAX_POLLS = 10', self.source)
        self.assertIn('attempt < DOCUMENT_MAX_POLLS', self.source)
        self.assertIn('/documents/${encodeURIComponent(documentId)}', self.source)
        self.assertIn('Analizando documento', self.source)
        self.assertIn('status === "UPLOADED"', self.source)
        self.assertIn('status === "FAILED"', self.source)
        self.assertIn('no superó el análisis de seguridad', self.source)
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('Actualizar estados', html)
        self.assertIn('Preparar para consulta (0)', html)

    def test_refresh_is_get_only_and_preparation_requires_uploaded(self) -> None:
        refresh_handler = self.source.split("async function refreshDocumentStatuses", 1)[1].split("async function prepareDocuments", 1)[0]
        prepare_handler = self.source.split("async function prepareDocuments", 1)[1].split("async function logout", 1)[0]
        self.assertIn("await loadDocuments(generation)", refresh_handler)
        self.assertNotIn("startAndPollIngestion", refresh_handler)
        self.assertIn('item.status === "UPLOADED"', prepare_handler)
        self.assertIn("MAX_DOCUMENTS_PER_INGESTION", prepare_handler)
        self.assertNotIn('item.status === "FAILED"', prepare_handler)

    def test_progress_is_visual_only_and_operational_status_is_announced_once(self) -> None:
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "frontend" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="upload-progress" class="upload-progress" hidden role="group"', html)
        self.assertNotIn('id="upload-progress" class="upload-progress" hidden role="status"', html)
        self.assertIn('id="app-status" class="app-message" role="status" aria-live="polite"', html)
        self.assertIn('.upload-progress[hidden]', styles)
        self.assertIn('.disclaimer[hidden]', styles)

    def test_workspace_navigation_and_progressive_review_form_keep_contracts(self) -> None:
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="workspace-nav"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('id="workspace-tab-consultation"', html)
        self.assertIn('id="workspace-tab-documents"', html)
        self.assertIn('id="workspace-tab-reviews"', html)
        self.assertIn('data-workspace-panel="consultation"', html)
        self.assertIn('class="control-group control-group-wide" data-panel="documents"', html)
        self.assertIn('id="review-form" class="review-form-panel" hidden', html)
        self.assertIn('aria-controls="review-form" aria-expanded="false"', html)
        self.assertIn('id="operator-json" class="operator-json" hidden', html)
        self.assertIn('function setReviewFormOpen(open, focus)', self.source)
        self.assertIn('La respuesta está disponible; el historial de actividad no se pudo actualizar.', self.source)

    def test_query_loading_feedback_preserves_context_and_restores_busy_state(self) -> None:
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
        citations = (Path(__file__).parents[1] / "frontend" / "citations.js").read_text(encoding="utf-8")
        styles = (Path(__file__).parents[1] / "frontend" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="ask-button" class="button button-primary" type="submit" disabled aria-busy="false">Consultar</button>', html)
        self.assertIn('id="answer" class="answer-copy" aria-live="polite" aria-busy="false"', html)
        self.assertIn("function setQueryLoadingState(active)", self.source)
        self.assertIn('askButton.textContent = active ? "Consultando…" : "Consultar"', self.source)
        self.assertIn('askButton.setAttribute("aria-busy", "true")', self.source)
        self.assertIn('answer.setAttribute("aria-busy", active ? "true" : "false")', self.source)
        self.assertIn("function renderLoadingState()", citations)
        self.assertIn("Buscando en los documentos autorizados…", citations)
        self.assertIn('status.dataset.status = "loading"', citations)
        self.assertIn('BUSCANDO RESPUESTA', citations)
        self.assertIn('answer.append(loading);', citations)
        self.assertIn('.status[data-status="loading"]', styles)
        self.assertIn('.answer-loading[hidden] { display: none; }', styles)
        self.assertIn("@keyframes answer-loading-enter", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertNotIn('setMessage("Consultando los documentos autorizados…"', self.source)

    def test_matter_change_cancels_inflight_upload_or_poll(self) -> None:
        self.assertIn("resetController();", self.source)
        self.assertIn("signal: state.controller.signal", self.source)
        self.assertIn("if (!isCurrent(generation)) return null;", self.source)

    def test_monthly_quota_error_is_explained_without_backend_code(self) -> None:
        self.assertIn("quota_exceeded", self.source)
        self.assertIn("límite mensual de la beta", self.source)


if __name__ == "__main__":
    unittest.main()
