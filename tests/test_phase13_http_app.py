from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.authorization import InMemoryAuthorizationStore
from legaldesk.documents import DocumentPipeline, InMemoryDocumentMetadataRepository, InMemoryObjectStorage
from legaldesk.domain.models import Matter, User
from legaldesk.http_app import ApplicationComposition, create_http_app
from legaldesk.identity import OidcTokenVerifier, OidcVerifierConfig
from legaldesk.memory import InMemoryConversationBindingStore, InMemoryShortTermMemory
from legaldesk.memory import derive_memory_scope_for_identity
from legaldesk.mcp_server import MCPServer


class _KeyResolver:
    def __init__(self, key):
        self.key = key

    def get_signing_key(self, _token):
        return self.key


class _Exchange:
    def __init__(self, token):
        self.token = token

    def exchange(self, code, *, code_verifier, redirect_uri):
        if code != "test-code" or not code_verifier or redirect_uri != "http://localhost:8000/callback":
            raise ValueError("exchange input")
        return self.token


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        return


class Phase13HttpAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private = private
        cls.public = private.public_key()

    def setUp(self):
        now = int(time.time())
        self.token = jwt.encode(
            {
                "sub": "subject-alice",
                "iss": "https://issuer.test",
                "exp": now + 600,
                "iat": now - 1,
                "token_use": "access",
                "client_id": "client",
                "scope": "openid legaldesk/use",
            },
            self.private,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        identity_verifier = OidcTokenVerifier(
            OidcVerifierConfig(issuer="https://issuer.test", client_id="client", required_scope="legaldesk/use", allowed_token_use=frozenset({"access"})),
            _KeyResolver(self.public),
        )
        self.auth = InMemoryAuthorizationStore(
            users_by_subject={"subject-alice": User("user-alice", "subject-alice", frozenset({"tenant-a"}))},
            matters_by_id={
                "matter-a": Matter("matter-a", "tenant-a", "Matter A", frozenset({"user-alice"})),
                "matter-foreign": Matter("matter-foreign", "tenant-a", "Foreign", frozenset()),
            },
        )
        self.storage = InMemoryObjectStorage()
        self.metadata = InMemoryDocumentMetadataRepository()
        self.memory = InMemoryShortTermMemory()
        self.conversations = InMemoryConversationBindingStore()
        self.chat_calls = []

        def chat(identity, *, matter_id, conversation_id, session_id, question, correlation_id, authorized_evidence_sink=None):
            self.chat_calls.append((identity.subject, matter_id, question, correlation_id))
            return {"answer": "synthetic test answer", "citations": [], "evidenceStatus": "answerable", "operationStatus": "ok"}

        composition = ApplicationComposition(
            identity_verifier=identity_verifier,
            token_exchange=_Exchange(self.token),
            authorization_store=self.auth,
            conversation_store=self.conversations,
            document_pipeline=DocumentPipeline(self.auth, self.storage, self.metadata),
            object_storage=self.storage,
            metadata_repository=self.metadata,
            mcp_server=MCPServer(self.metadata),
            memory=self.memory,
            chat_service=chat,
            sync_service=lambda **kwargs: {"status": "COMPLETE"},
            matter_catalog=("matter-a", "matter-foreign"),
            authorization_endpoint="https://issuer.test/authorize",
            oauth_client_id="client",
        )
        self.app = create_http_app(composition)
        self.server = make_server("127.0.0.1", 0, self.app, handler_class=_QuietRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.cookies = {}
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, method, path, body=None, *, origin=None, csrf=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if origin is not None:
            headers["Origin"] = origin
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, encoded, headers)
        response = connection.getresponse()
        payload = response.read()
        for value in response.headers.get_all("Set-Cookie", []):
            pair = value.split(";", 1)[0]
            key, _, item = pair.partition("=")
            if item:
                self.cookies[key] = item
            else:
                self.cookies.pop(key, None)
        try:
            decoded = json.loads(payload.decode()) if payload else {}
        except json.JSONDecodeError:
            decoded = payload.decode(errors="replace")
        connection.close()
        return response.status, decoded, response.headers

    def login(self):
        status, _, headers = self.request("GET", "/login")
        self.assertEqual(status, 302)
        location = headers["Location"]
        self.assertNotIn(self.token, location)
        state = parse_qs(urlsplit(location).query)["state"][0]
        status, _, _ = self.request("GET", f"/callback?state={state}&code=test-code")
        self.assertEqual(status, 302)
        status, body, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.csrf = body["csrfToken"]

    def test_loopback_http_auth_scope_csrf_and_history(self):
        self.login()
        status, body, _ = self.request("GET", "/api/matters")
        self.assertEqual(status, 200)
        self.assertEqual([item["matterId"] for item in body["matters"]], ["matter-a"])

        status, _, _ = self.request("POST", "/api/conversations", {"matterId": "matter-a"})
        self.assertEqual(status, 403)  # CSRF is mandatory for writes.
        status, conversation, _ = self.request("POST", "/api/conversations", {"matterId": "matter-a"}, csrf=self.csrf)
        self.assertEqual(status, 201)
        scope = {key: conversation[key] for key in ("matterId", "conversationId", "sessionId")}

        status, _, _ = self.request("POST", "/api/chat", {**scope, "question": "What is authorized?"}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.chat_calls), 1)
        status, history, _ = self.request("GET", f"/api/conversations/{conversation['conversationId']}")
        self.assertEqual(status, 200)
        self.assertEqual([item["role"] for item in history["events"]], ["USER", "ASSISTANT"])
        # A provider/tool event with the same actor/session is not application
        # history unless this app recorded its accepted event ID.
        identity = self.app.sessions[next(iter(self.app.sessions))].identity
        context = self.app._context(identity, "matter-a")
        scope_obj = derive_memory_scope_for_identity(identity, "matter-a", conversation["conversationId"], conversation["sessionId"], self.auth, self.conversations, correlation_id=context.correlation_id)
        self.memory.append_event(scope_obj, role="ASSISTANT", text="rejected provider output")
        status, history, _ = self.request("GET", f"/api/conversations/{conversation['conversationId']}")
        self.assertEqual(status, 200)
        self.assertEqual([item["role"] for item in history["events"]], ["USER", "ASSISTANT"])

    def test_responses_include_browser_security_headers(self):
        status, _body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("camera=()", headers["Permissions-Policy"])

    def test_loopback_http_rejects_foreign_scope_origin_unknown_routes_and_expired_session(self):
        self.login()
        status, _, _ = self.request("POST", "/api/conversations", {"matterId": "matter-a"}, origin="https://evil.test", csrf=self.csrf)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/conversations", {"matterId": "matter-foreign"}, csrf=self.csrf)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/citations?handle=unknown")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/uploads", {}, csrf=self.csrf)
        self.assertNotEqual(status, 200)
        for key, record in list(self.app.sessions.items()):
            self.app.sessions[key] = type(record)(record.identity, record.access_token, record.csrf_token, time.time() - 1)
        status, _, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
