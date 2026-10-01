from __future__ import annotations

import base64
import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from uuid import UUID
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import legaldesk.api_gateway as adapter
from legaldesk.api_gateway import lambda_handler
from legaldesk.authorization import AuthorizationDenied
from legaldesk.http_app import ApplicationComposition, MAX_HTTP_BODY, LoopbackLegalDeskApp, TRUSTED_EDGE_HEADER
from legaldesk.observability import InMemoryTelemetrySink
from legaldesk.quota import InMemoryQuotaLedger


def _event(*, path: str = "/api/me", method: str = "GET", headers: dict[str, str] | None = None, body: str | None = None, encoded: bool = False, query: str = "") -> dict[str, object]:
    values = {"Host": "public.example"}
    values[TRUSTED_EDGE_HEADER] = "edge-secret"
    values.update(headers or {})
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": query,
        "headers": values,
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "isBase64Encoded": encoded,
    }


class Phase14ApiGatewayTests(unittest.TestCase):
    def tearDown(self) -> None:
        adapter.reset_application_for_tests()

    def test_v2_adapter_preserves_multi_cookie_response(self) -> None:
        observed: dict[str, object] = {}

        def app(environ, start_response):
            observed.update(environ)
            start_response("302 Found", [
                ("Content-Type", "application/json"),
                ("Set-Cookie", "one=1; HttpOnly"),
                ("Set-Cookie", "two=2; HttpOnly"),
            ])
            return [b"{}"]

        class StubApplication:
            composition = type("Composition", (), {"public_mode": True, "trusted_edge_value": "edge-secret"})()

            def __call__(self, environ, start_response):
                return app(environ, start_response)

        adapter._app = StubApplication()
        response = lambda_handler(_event(path="/callback", method="GET", query="state=s&code=c"), None)
        self.assertEqual(response["statusCode"], 302)
        self.assertEqual(response["cookies"], ["one=1; HttpOnly", "two=2; HttpOnly"])
        self.assertNotIn("set-cookie", response["headers"])
        self.assertEqual(observed["QUERY_STRING"], "state=s&code=c")
        self.assertEqual(observed["HTTP_HOST"], "public.example")

    def test_base64_body_limit_and_malformed_headers_fail_before_composition(self) -> None:
        with patch.object(adapter, "build_aws_composition") as factory:
            too_large = base64.b64encode(b"x" * (MAX_HTTP_BODY + 1)).decode("ascii")
            response = lambda_handler(_event(path="/api/upload", method="POST", body=too_large, encoded=True), None)
            self.assertEqual(response["statusCode"], 400)
            factory.assert_not_called()

        duplicate = _event(headers={"host": "public.example"})
        self.assertEqual(lambda_handler(duplicate, None)["statusCode"], 400)
        malformed = _event(headers={"X-Test": "bad\nvalue"})
        self.assertEqual(lambda_handler(malformed, None)["statusCode"], 400)
        malformed_query = _event(query="state=%")
        self.assertEqual(lambda_handler(malformed_query, None)["statusCode"], 400)
        malformed_host = _event(headers={"Host": "public example"})
        self.assertEqual(lambda_handler(malformed_host, None)["statusCode"], 400)
        duplicate_cookie = _event(headers={"Cookie": "a=1"})
        duplicate_cookie["cookies"] = ["b=2"]
        self.assertEqual(lambda_handler(duplicate_cookie, None)["statusCode"], 400)

    def test_lazy_composition_has_no_fallback_and_errors_are_generic(self) -> None:
        calls = []

        def factory(*, allow_aws, config):
            calls.append(allow_aws)
            raise RuntimeError("provider secret")

        config = type("PublicConfig", (), {"public_mode": True, "trusted_edge_value": "edge-secret"})()
        sink = InMemoryTelemetrySink()
        with patch.object(adapter, "AWSResourceConfig") as config_factory, patch.object(adapter, "build_aws_composition", side_effect=factory), patch.object(adapter, "DEFAULT_TELEMETRY_SINK", sink):
            config_factory.from_environment.return_value = config
            response = lambda_handler(_event(), None)
        self.assertEqual(response["statusCode"], 500)
        self.assertNotIn("provider secret", response["body"])
        payload = json.loads(response["body"])
        self.assertEqual(payload["error"], "operation_failed")
        UUID(payload["errorId"])
        self.assertEqual(sink.events[0].to_dict()["error_code"], "internal_error")
        self.assertNotIn("provider secret", sink.events[0].to_dict())
        self.assertEqual(calls, [True])

        adapter.reset_application_for_tests()
        config = type("LoopbackConfig", (), {"public_mode": False, "trusted_edge_value": None})()
        with patch.object(adapter, "AWSResourceConfig") as config_factory, patch.object(adapter, "build_aws_composition") as factory:
            config_factory.from_environment.return_value = config
            response = lambda_handler(_event(), None)
        self.assertEqual(response["statusCode"], 500)
        factory.assert_not_called()

    def test_public_mode_requires_exact_host_origin_and_secure_cookies(self) -> None:
        composition = ApplicationComposition(
            identity_verifier=object(),
            token_exchange=None,
            authorization_store=object(),
            conversation_store=object(),
            document_pipeline=object(),
            object_storage=object(),
            metadata_repository=object(),
            mcp_server=object(),
            matter_catalog=("matter-a",),
            authorization_endpoint="https://issuer.example/authorize",
            oauth_client_id="client",
            public_base_url="https://public.example",
            redirect_uri="https://public.example/callback",
            allowed_hosts=frozenset({"public.example"}),
            allowed_origins=frozenset({"https://public.example"}),
            secure_cookies=True,
            public_mode=True,
            trusted_edge_value="edge-secret",
            quota=InMemoryQuotaLedger(),
        )
        adapter._app = LoopbackLegalDeskApp(composition)
        login = lambda_handler(_event(path="/login"), None)
        self.assertEqual(login["statusCode"], 302)
        self.assertIn("Secure", login["cookies"][0])
        self.assertIn("strict-transport-security", login["headers"])

        wrong_host = lambda_handler(_event(path="/login", headers={"Host": "evil.example"}), None)
        self.assertEqual(wrong_host["statusCode"], 403)
        wrong_origin = lambda_handler(_event(path="/api/me", headers={"Origin": "https://evil.example"}), None)
        self.assertEqual(wrong_origin["statusCode"], 403)

    def test_public_safe_get_may_omit_origin_but_mutation_requires_exact_origin(self) -> None:
        composition = ApplicationComposition(
            identity_verifier=object(),
            token_exchange=None,
            authorization_store=object(),
            conversation_store=object(),
            document_pipeline=object(),
            object_storage=object(),
            metadata_repository=object(),
            mcp_server=object(),
            matter_catalog=("matter-a",),
            authorization_endpoint="https://issuer.example/authorize",
            oauth_client_id="client",
            public_base_url="https://public.example",
            redirect_uri="https://public.example/callback",
            allowed_hosts=frozenset({"api.example"}),
            allowed_origins=frozenset({"https://public.example"}),
            secure_cookies=True,
            public_mode=True,
            trusted_edge_value="edge-secret",
            quota=InMemoryQuotaLedger(),
        )
        app = LoopbackLegalDeskApp(composition)
        base_environ = {
            "HTTP_HOST": "api.example",
            "legaldesk.edge_verified": True,
        }
        # Browsers may omit Origin on same-origin safe requests.
        app._request_guards(base_environ, session=None, mutating=False)
        # Mutating requests must carry the exact browser origin before any
        # session/CSRF processing is allowed.
        with self.assertRaises(AuthorizationDenied):
            app._request_guards(base_environ, session=None, mutating=True)
        exact_origin = {**base_environ, "HTTP_ORIGIN": "https://public.example"}
        app._request_guards(exact_origin, session=None, mutating=False)
        session = SimpleNamespace(csrf_token="csrf-secret")
        with self.assertRaises(AuthorizationDenied):
            app._request_guards(exact_origin, session=session, mutating=True)
        app._request_guards(
            {**exact_origin, "HTTP_X_CSRF_TOKEN": "csrf-secret"},
            session=session,
            mutating=True,
        )

    def test_public_mode_requires_explicit_quota_dependency(self) -> None:
        composition = ApplicationComposition(
            identity_verifier=object(), token_exchange=None, authorization_store=object(),
            conversation_store=object(), document_pipeline=object(), object_storage=object(),
            metadata_repository=object(), mcp_server=object(), matter_catalog=("matter-a",),
            authorization_endpoint="https://issuer.example/authorize", oauth_client_id="client",
            public_base_url="https://public.example", redirect_uri="https://public.example/callback",
            allowed_hosts=frozenset({"public.example"}), allowed_origins=frozenset({"https://public.example"}),
            secure_cookies=True, public_mode=True, trusted_edge_value="edge-secret",
            quota=InMemoryQuotaLedger(),
        )
        with self.assertRaises(ValueError):
            replace(composition, quota=None)

    def test_import_and_invalid_events_do_not_construct_aws(self) -> None:
        adapter.reset_application_for_tests()
        with patch.object(adapter, "build_aws_composition") as factory:
            response = lambda_handler({"version": "1.0"}, None)
            static_response = lambda_handler(_event(path="/app.js"), None)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(static_response["statusCode"], 400)
        factory.assert_not_called()

    def test_trusted_edge_marker_is_required_and_not_forwarded(self) -> None:
        observed: dict[str, object] = {}

        def app(environ, start_response):
            observed.update(environ)
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b"{}"]

        class StubApplication:
            composition = type("Composition", (), {"public_mode": True, "trusted_edge_value": "edge-secret"})()

            def __call__(self, environ, start_response):
                return app(environ, start_response)

        adapter._app = StubApplication()
        missing = _event()
        del missing["headers"][TRUSTED_EDGE_HEADER]
        self.assertEqual(lambda_handler(missing, None)["statusCode"], 403)
        wrong = _event(headers={TRUSTED_EDGE_HEADER: "wrong-secret"})
        self.assertEqual(lambda_handler(wrong, None)["statusCode"], 403)
        accepted = lambda_handler(_event(), None)
        self.assertEqual(accepted["statusCode"], 200)
        self.assertNotIn("HTTP_X_LEGALDESK_TRUSTED_EDGE", observed)
        self.assertTrue(observed.get("legaldesk.edge_verified"))


if __name__ == "__main__":
    unittest.main()
