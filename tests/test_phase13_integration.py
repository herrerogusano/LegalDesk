from __future__ import annotations

import http.client
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from legaldesk.application import build_aws_composition
from legaldesk.http_app import create_http_app
from legaldesk.observability import InMemoryTelemetrySink
from phase13_fixture import FakeTokenExchange, Phase13ProviderFixture


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        return


class Phase13ConcreteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Phase13ProviderFixture(ROOT)
        FakeTokenExchange.token = self.fixture.token

        def client(service_name, **_kwargs):
            return {
                "s3": self.fixture.s3,
                "bedrock-runtime": self.fixture.runtime,
                "bedrock-agent-runtime": self.fixture.runtime,
                "bedrock-agent": self.fixture.ingestion,
                "bedrock-agentcore": self.fixture.agentcore,
            }[service_name]

        with patch("legaldesk.application.DEFAULT_TELEMETRY_SINK", InMemoryTelemetrySink()), patch("boto3.resource", return_value=type("Resource", (), {"Table": lambda _self, _name: self.fixture.table})()), patch("boto3.client", side_effect=client), patch("legaldesk.application.PyJwtJwksKeyResolver", return_value=type("Resolver", (), {"get_signing_key": lambda _self, _token: self.fixture.public_key})()), patch("legaldesk.application.CognitoPkceTokenExchange", FakeTokenExchange):
            composition = build_aws_composition(allow_aws=True, config=self.fixture.config())
        from legaldesk.gateway_client import DirectGatewayInvoker
        from phase13_gateway_fixture import LocalGatewayTransport
        composition.gateway_invoker = DirectGatewayInvoker(
            composition.gateway_url,
            transport=LocalGatewayTransport(composition),
        )
        self.app = create_http_app(composition)
        self.server = make_server("127.0.0.1", 0, self.app, handler_class=_QuietRequestHandler)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.cookies = {}
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.fixture.close()

    def request(self, method, path, body=None, *, csrf=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        raw = response.read()
        for cookie in response.headers.get_all("Set-Cookie", []):
            key, _, value = cookie.split(";", 1)[0].partition("=")
            if value:
                self.cookies[key] = value
            else:
                self.cookies.pop(key, None)
        data = json.loads(raw.decode()) if raw else {}
        headers_out = response.headers
        status = response.status
        connection.close()
        return status, data, headers_out

    def login(self):
        status, _, headers = self.request("GET", "/login")
        self.assertEqual(status, 302)
        state = parse_qs(urlsplit(headers["Location"]).query)["state"][0]
        status, _, _ = self.request("GET", f"/callback?state={state}&code=integration-code")
        self.assertEqual(status, 302)
        status, body, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.csrf = body["csrfToken"]

    def test_real_composition_upload_sync_chat_citation_history_audit(self):
        self.login()
        status, conversation, _ = self.request("POST", "/api/conversations", {"matterId": "matter-integration"}, csrf=self.csrf)
        self.assertEqual(status, 201)
        scope = {key: conversation[key] for key in ("matterId", "conversationId", "sessionId")}

        body = b"The inspection period is four years."
        status, authorization, _ = self.request("POST", "/api/matters/matter-integration/documents/upload-authorizations", {"filename": "evidence.txt", "mediaType": "text/plain", "fileSizeBytes": len(body)}, csrf=self.csrf)
        self.assertEqual(status, 201)
        upload_request = Request(authorization["presignedUrl"], data=body, method="PUT", headers=authorization["headers"])
        with urlopen(upload_request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        document_id = authorization["document"]["documentId"]
        status, confirmed, _ = self.request("POST", f"/api/matters/matter-integration/documents/{document_id}/confirm", {}, csrf=self.csrf)
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["document"]["status"], "UPLOADED")

        status, processing, _ = self.request("POST", "/api/matters/matter-integration/sync", {"documentIds": [document_id]}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(processing["operationStatus"], "documents_indexed")
        status, document, _ = self.request("GET", f"/api/matters/matter-integration/documents/{document_id}")
        self.assertEqual(status, 200)
        self.assertEqual(document["document"]["status"], "INDEXED")

        status, answer, _ = self.request("POST", "/api/chat", {**scope, "question": "What is the inspection period?"}, csrf=self.csrf)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["evidenceStatus"], "answerable")
        self.assertEqual(len(answer["citations"]), 1)
        self.assertNotIn("sourceUri", answer["citations"][0])
        self.assertIsInstance(answer["citations"][0].get("handle"), str)
        status, citation, _ = self.request("GET", f"/api/citations?handle={answer['citations'][0]['handle']}")
        self.assertEqual(status, 200)
        self.assertNotIn("sourceUri", citation)
        status, history, _ = self.request("GET", f"/api/conversations/{scope['conversationId']}?sessionId={scope['sessionId']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["events"]), 2)
        status, audit, _ = self.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(event.get("operation") == "chat" for event in audit["events"]))
        self.assertFalse(any("fictional integration evidence" in json.dumps(event) for event in audit["events"]))


if __name__ == "__main__":
    unittest.main()
