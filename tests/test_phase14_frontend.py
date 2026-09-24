from __future__ import annotations

import unittest
from pathlib import Path


class Phase14FrontendIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text(encoding="utf-8")

    def test_public_frontend_uses_start_status_and_bounded_polling(self) -> None:
        self.assertIn("/ingestions`,", self.source)
        self.assertIn("/ingestions/${encodeURIComponent(operationId)}", self.source)
        self.assertIn("const INGESTION_MAX_POLLS = 20", self.source)
        self.assertIn("attempt < INGESTION_MAX_POLLS", self.source)
        self.assertIn("La indexación está tardando más de lo esperado", self.source)
        self.assertNotIn("/sync", self.source)

    def test_document_analysis_is_bounded_and_ingestion_requires_uploaded(self) -> None:
        self.assertIn('const DOCUMENT_MAX_POLLS = 10', self.source)
        self.assertIn('attempt < DOCUMENT_MAX_POLLS', self.source)
        self.assertIn('/documents/${encodeURIComponent(documentId)}', self.source)
        self.assertIn('Analizando documento', self.source)
        self.assertIn('status === "UPLOADED"', self.source)
        self.assertIn('status === "FAILED"', self.source)
        self.assertIn('no superó el análisis de seguridad', self.source)
        self.assertIn('Comprobar estado', (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8"))

    def test_failed_documents_are_never_selected_for_retry_ingestion(self) -> None:
        sync_handler = self.source.split('$("sync-button").addEventListener', 1)[1]
        self.assertIn('item.status === "UPLOADED"', sync_handler)
        self.assertNotIn('["UPLOADED", "FAILED"]', sync_handler)

    def test_progress_is_visual_only_and_operational_status_is_announced_once(self) -> None:
        html = (Path(__file__).parents[1] / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="upload-progress" class="upload-progress" hidden role="group"', html)
        self.assertNotIn('id="upload-progress" class="upload-progress" hidden role="status"', html)
        self.assertIn('id="app-status" class="app-message" role="status" aria-live="polite"', html)

    def test_matter_change_cancels_inflight_upload_or_poll(self) -> None:
        self.assertIn("resetController();", self.source)
        self.assertIn("signal: state.controller.signal", self.source)
        self.assertIn("if (!isCurrent(generation)) return null;", self.source)

    def test_monthly_quota_error_is_explained_without_backend_code(self) -> None:
        self.assertIn("quota_exceeded", self.source)
        self.assertIn("límite mensual de la beta", self.source)


if __name__ == "__main__":
    unittest.main()
