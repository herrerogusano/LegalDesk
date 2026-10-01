from __future__ import annotations

import http.client
import io
import json
import sys
import threading
import time
from dataclasses import replace
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
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
from legaldesk.quota import InMemoryQuotaLedger, QuotaLimits


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

    def request(self, method, path, body=None, *, origin=None, csrf=None, custom_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if origin is not None:
            headers["Origin"] = origin
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if custom_headers:
            headers.update(custom_headers)
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

    def test_quota_exhaustion_returns_generic_429_without_calling_chat_provider(self):
        self.login()
        self.app.composition.quota = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=1))
        status, conversation, _ = self.request("POST", "/api/conversations", {"matterId": "matter-a"}, csrf=self.csrf)
        self.assertEqual(status, 201)
        scope = {key: conversation[key] for key in ("matterId", "conversationId", "sessionId")}
        status, _, _ = self.request("POST", "/api/chat", {**scope, "question": "first"}, csrf=self.csrf)
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", "/api/chat", {**scope, "question": "second"}, csrf=self.csrf)
        self.assertEqual(status, 429)
        self.assertEqual(body, {"error": "quota_exceeded"})
        self.assertEqual(len(self.chat_calls), 1)

    def test_invalid_upload_does_not_consume_quota(self):
        self.login()
        self.app.composition.quota = InMemoryQuotaLedger(
            limits=QuotaLimits(uploads_per_month=1, upload_bytes_per_month=10)
        )
        status, _, _ = self.request(
            "POST", "/api/matters/matter-a/documents/upload-authorizations",
            {"filename": "bad.txt", "mediaType": "text/plain"}, csrf=self.csrf,
        )
        self.assertEqual(status, 400)
        valid = {
            "filename": "good.txt", "mediaType": "text/plain", "fileSizeBytes": 4,
            "jurisdiction": "fictional", "documentDate": "2099-01-01",
            "confidentiality": "fictional-internal",
        }
        status, _, _ = self.request(
            "POST", "/api/matters/matter-a/documents/upload-authorizations", valid, csrf=self.csrf,
        )
        self.assertEqual(status, 201)
        status, body, _ = self.request(
            "POST", "/api/matters/matter-a/documents/upload-authorizations", valid, csrf=self.csrf,
        )
        self.assertEqual(status, 429)
        self.assertEqual(body, {"error": "quota_exceeded"})

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

    def test_logout_invalidates_local_session_and_builds_server_provider_redirect(self):
        self.login()
        status, _, _ = self.request("GET", "/logout")
        self.assertEqual(status, 302)
        self.assertEqual(self.app.sessions and len(self.app.sessions), 1)
        status, body, headers = self.request("POST", "/logout", {}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        logout_url = body["logoutUrl"]
        self.assertEqual(
            logout_url,
            "https://issuer.test/logout?client_id=client&logout_uri=http%3A%2F%2Flocalhost%3A8000%2Flogout",
        )
        self.assertNotIn(self.token, logout_url)
        self.assertNotIn("access_token", logout_url)
        self.assertEqual(headers.get("Set-Cookie", "").split(";", 1)[0], "legaldesk_session=")
        status, _, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 403)
        status, _, headers = self.request("GET", "/logout")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")

    def test_logout_get_is_non_destructive_and_csrf_fail_closed(self):
        self.login()
        status, _, _ = self.request("POST", "/logout", {})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.app.sessions), 1)
        status, _, headers = self.request("GET", "/logout")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        status, body, _ = self.request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(body["subject"], "subject-alice")

    def test_logout_accepts_expired_jwt_when_local_record_and_csrf_are_valid(self):
        self.login()
        key = next(iter(self.app.sessions))
        record = self.app.sessions[key]
        # The local session is still live, but the IdP token is no longer
        # usable.  Logout must not re-run the application JWT authorization.
        expired_token = jwt.encode(
            {
                "sub": "subject-alice", "iss": "https://issuer.test", "exp": int(time.time()) - 1,
                "token_use": "access", "client_id": "client", "scope": "openid legaldesk/use",
            },
            self.private, algorithm="RS256", headers={"kid": "test-key"},
        )
        self.app.sessions[key] = type(record)(record.identity, expired_token, record.csrf_token, record.expires_at)
        status, body, _ = self.request("POST", "/logout", {}, csrf=self.csrf)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn(key, self.app.sessions)

    def test_logout_expired_or_missing_record_requires_origin_and_custom_intent(self):
        self.login()
        key = next(iter(self.app.sessions))
        record = self.app.sessions[key]
        self.app.sessions[key] = type(record)(record.identity, record.access_token, record.csrf_token, time.time() - 1)
        marker = {"X-LegalDesk-Logout": "1"}

        status, _, _ = self.request("POST", "/logout", {}, custom_headers=marker)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/logout", {}, origin="http://localhost:8000")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/logout", {}, origin="http://localhost:8000", csrf="stale-csrf-marker")
        self.assertEqual(status, 200)
        self.cookies = {"legaldesk_session": "missing-session"}
        status, _, _ = self.request("POST", "/logout", {}, origin="http://localhost:8000", custom_headers={"X-LegalDesk-Logout": "0"})
        self.assertEqual(status, 403)
        status, body, headers = self.request("POST", "/logout", {}, origin="http://localhost:8000", custom_headers=marker)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(headers.get("Set-Cookie", "").split(";", 1)[0], "legaldesk_session=")

        self.cookies = {"legaldesk_session": "missing-session"}
        status, _, _ = self.request("POST", "/logout", {}, origin="http://localhost:8000", custom_headers={"X-LegalDesk-Logout": "1", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/logout", {}, origin="null", custom_headers=marker)
        self.assertEqual(status, 403)
        status, body, _ = self.request("POST", "/logout", {}, origin="http://localhost:8000", custom_headers=marker)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_logout_active_record_rejects_invalid_csrf_even_with_anonymous_marker(self):
        self.login()
        status, _, _ = self.request(
            "POST", "/logout", {}, origin="http://localhost:8000",
            csrf="wrong", custom_headers={"X-LegalDesk-Logout": "1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.app.sessions), 1)

    def test_logout_storage_failure_fails_closed_without_cookie_or_provider_url(self):
        class BrokenStore:
            def get_session(self, _key):
                raise RuntimeError("storage unavailable")

        self.app.state_store = BrokenStore()
        self.cookies = {"legaldesk_session": "opaque-session"}
        status, body, headers = self.request(
            "POST", "/logout", {}, origin="http://localhost:8000",
            custom_headers={"X-LegalDesk-Logout": "1"},
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "operation_failed")
        UUID(body["errorId"])
        self.assertNotIn("storage unavailable", json.dumps(body))
        self.assertNotIn("Set-Cookie", headers)

    def test_logout_delete_failure_fails_closed_without_clearing_cookie(self):
        self.login()
        key = next(iter(self.app.sessions))
        record = self.app.sessions[key]

        class DeleteBrokenStore:
            def get_session(self, requested_key):
                return record if requested_key == key else None

            def delete_session(self, _requested_key):
                raise RuntimeError("delete unavailable")

        self.app.state_store = DeleteBrokenStore()
        status, body, headers = self.request("POST", "/logout", {}, csrf=self.csrf)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "operation_failed")
        UUID(body["errorId"])
        self.assertNotIn("delete unavailable", json.dumps(body))
        self.assertNotIn("Set-Cookie", headers)

    def test_public_logout_fallback_keeps_trusted_edge_host_and_origin_guards(self):
        self.login()
        composition = replace(
            self.app.composition,
            public_mode=True,
            secure_cookies=True,
            public_base_url="https://beta.example.com",
            redirect_uri="https://beta.example.com/callback",
            allowed_hosts=frozenset({"api.example.com"}),
            allowed_origins=frozenset({"https://beta.example.com"}),
            trusted_edge_value="edge-secret",
            quota=InMemoryQuotaLedger(),
        )
        public_app = create_http_app(composition)

        def call(method, path, *, host="api.example.com", origin=None, cookie="missing-session", csrf=None, edge=True):
            environ = {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "HTTP_HOST": host,
                "HTTP_COOKIE": f"legaldesk_session={cookie}" if cookie else "",
                "wsgi.input": io.BytesIO(b"{}"),
                "CONTENT_LENGTH": "2",
            }
            if origin is not None:
                environ["HTTP_ORIGIN"] = origin
            if csrf is not None:
                environ["HTTP_X_CSRF_TOKEN"] = csrf
            if edge:
                environ["legaldesk.edge_verified"] = True
            captured = {}

            def start_response(status, headers):
                captured["status"] = int(status.split(" ", 1)[0])
                captured["headers"] = dict(headers)

            payload = b"".join(public_app(environ, start_response))
            return captured["status"], json.loads(payload.decode()), captured["headers"]

        key = next(iter(self.app.sessions))
        record = self.app.sessions[key]
        expired_token = jwt.encode(
            {
                "sub": "subject-alice", "iss": "https://issuer.test", "exp": int(time.time()) - 1,
                "token_use": "access", "client_id": "client", "scope": "openid legaldesk/use",
            },
            self.private, algorithm="RS256", headers={"kid": "test-key"},
        )
        self.app.sessions[key] = type(record)(record.identity, expired_token, record.csrf_token, record.expires_at)
        status, _, _ = call("GET", "/api/me", cookie=key, origin="https://beta.example.com")
        self.assertEqual(status, 403)
        # The failed JWT verification removed the record; it must not make any
        # other authenticated getter available before the logout fallback.
        status, _, _ = call("GET", "/api/me", cookie=key, origin="https://beta.example.com")
        self.assertEqual(status, 403)

        marker = "stale-marker"
        status, _, _ = call("POST", "/logout", host="evil.example.com", origin="https://beta.example.com", csrf=marker)
        self.assertEqual(status, 403)
        status, _, _ = call("POST", "/logout", origin="https://evil.example.com", csrf=marker)
        self.assertEqual(status, 403)
        status, _, _ = call("POST", "/logout", origin=None, csrf=marker)
        self.assertEqual(status, 403)
        status, _, _ = call("POST", "/logout", origin="https://beta.example.com", csrf=marker, edge=False)
        self.assertEqual(status, 403)
        status, body, headers = call("POST", "/logout", origin="https://beta.example.com", csrf=marker)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(headers["Set-Cookie"].split(";", 1)[0], "legaldesk_session=")


if __name__ == "__main__":
    unittest.main()
