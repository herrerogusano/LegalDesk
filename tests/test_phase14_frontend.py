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

    def test_matter_change_cancels_inflight_upload_or_poll(self) -> None:
        self.assertIn("resetController();", self.source)
        self.assertIn("signal: state.controller.signal", self.source)
        self.assertIn("if (!isCurrent(generation)) return null;", self.source)


if __name__ == "__main__":
    unittest.main()
