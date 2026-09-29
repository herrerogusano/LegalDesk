from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.http_app import LoopbackLegalDeskApp  # noqa: E402
from legaldesk.mcp_server import MCPServer  # noqa: E402


class DocumentListingSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document(
            document_id="doc-12345678",
            matter_id="matter-a",
            tenant_id="tenant-a",
            name="manual-demo-evidence.txt",
            s3_key="tenants/tenant-a/matters/matter-a/documents/doc-12345678/original.txt",
            media_type="text/plain",
            jurisdiction="fictional",
            document_date="2099-01-01",
            confidentiality="fictional-internal",
            status=DocumentStatus.PENDING_UPLOAD,
            file_size_bytes=38,
            uploaded_at=datetime(2026, 9, 29, 12, 34, 56, tzinfo=timezone.utc),
            malware_scan_status=MalwareScanStatus.PENDING,
        )

    def test_http_document_serialization_exposes_safe_creation_metadata(self) -> None:
        payload = LoopbackLegalDeskApp._document(self.document)
        self.assertEqual(payload["status"], "PENDING_UPLOAD")
        self.assertEqual(payload["fileSizeBytes"], 38)
        self.assertEqual(payload["uploadedAt"], "2026-09-29T12:34:56+00:00")
        self.assertNotIn("s3Key", payload)

    def test_mcp_document_serialization_exposes_safe_creation_metadata(self) -> None:
        context = SimpleNamespace(
            tenant_id="tenant-a",
            matter_id="matter-a",
            correlation_id="corr-a",
        )
        with patch("legaldesk.mcp_server.require_authorized_context", return_value=context):
            payload = MCPServer._safe_document(self.document, context)
        self.assertEqual(payload["fileSizeBytes"], 38)
        self.assertEqual(payload["uploadedAt"], "2026-09-29T12:34:56+00:00")
        self.assertNotIn("s3Key", payload)


if __name__ == "__main__":
    unittest.main()
