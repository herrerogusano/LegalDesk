from pathlib import Path
import re
import unittest


class UploadCorsTests(unittest.TestCase):
    def test_presigned_upload_cors_is_origin_scoped_and_put_only(self):
        template = (Path(__file__).parents[1] / "infra/cloudformation/phase-02-document-pipeline.yaml").read_text()
        origin = template.split("  ApplicationOrigin:", 1)[1].split("\nResources:", 1)[0]
        pattern = re.search(r"AllowedPattern: '([^']+)'", origin).group(1)
        for valid in ("http://localhost:8000", "https://demo.example.test"):
            self.assertIsNotNone(re.fullmatch(pattern, valid))
        for invalid in ("*", "https://*.example.test", "https://demo.test/path", "null"):
            self.assertIsNone(re.fullmatch(pattern, invalid))
        cors = template.split("      CorsConfiguration:", 1)[1].split("      BucketEncryption:", 1)[0]
        self.assertIn("- !Ref ApplicationOrigin", cors)
        self.assertEqual(re.findall(r"^\s+- ([A-Z]+)$", cors, re.MULTILINE), ["PUT"])
        self.assertNotIn('"*"', cors)
        self.assertIn("- Content-Type", cors)
        for header in ("tenant-id", "matter-id", "document-id"):
            self.assertIn(f"- x-amz-meta-{header}", cors)
        self.assertIn("- x-amz-server-side-encryption", cors)
        # CORS is not authorization: public access and TLS controls remain.
        self.assertIn("BlockPublicPolicy: true", template)
        self.assertIn('aws:SecureTransport: "false"', template)
