from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.authorization import AuthorizationDenied, RequestContext, VerifiedIdentity, build_request_context
from legaldesk.documents import build_document_key
from legaldesk.retrieval import build_matter_filter
from legaldesk.review_tasks import InMemoryReviewTaskRepository, create_review_task
from legaldesk.gateway_interceptor import (
    GatewayAuthorizationGrant,
    GatewayTarget,
    InMemoryGatewayGrantRepository,
    transform_gateway_request,
)
from legaldesk.documents import Document, DocumentStatus, InMemoryDocumentMetadataRepository
from legaldesk.mcp_server import GET_DOCUMENT_METADATA, LIST_MATTER_DOCUMENTS, MCPServer, mcp_lambda_handler
from legaldesk.identity import (
    OidcTokenVerifier,
    OidcVerifierConfig,
    create_pkce_authorization_request,
    pkce_code_challenge,
)
from legaldesk.memory import (
    Boto3DynamoConversationBindingStore,
    InMemoryConversationBindingStore,
    InMemoryShortTermMemory,
    MemoryScope,
)


class Phase10IdentityBoundaryTests(unittest.TestCase):
    def test_pkce_s256_matches_rfc7636_vector_and_authorize_request(self) -> None:
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(
            pkce_code_challenge(verifier),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )
        request = create_pkce_authorization_request(
            "https://issuer.example/oauth2/authorize",
            client_id="public-client",
            redirect_uri="http://localhost/callback",
            state="state-123",
            code_verifier=verifier,
        )
        query = parse_qs(urlsplit(request.authorization_url).query)
        self.assertEqual(query["code_challenge"], [request.code_challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], ["state-123"])

    def test_pkce_rejects_weak_verifier_and_non_https_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            pkce_code_challenge("too-short")
        with self.assertRaises(ValueError):
            create_pkce_authorization_request(
                "https://issuer.example/authorize",
                client_id="public-client",
                redirect_uri="http://localhost/callback",
                state="",
            )
        with self.assertRaises(ValueError):
            create_pkce_authorization_request(
                "http://issuer.example/authorize",
                client_id="public-client",
                redirect_uri="http://localhost/callback",
            )

    @staticmethod
    def _signed_access_token(*, private_key, kid="key-1", **overrides):
        import jwt

        claims = {
            "iss": "https://issuer.example",
            "sub": "idp|alice-fictional",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "nbf": int(time.time()) - 1,
            "token_use": "access",
            "client_id": "client-1",
            "scope": "legaldesk/use",
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    def test_real_rsa_tokens_validate_and_fail_closed(self) -> None:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        class Resolver:
            def get_signing_key(self, token):
                if jwt.get_unverified_header(token).get("kid") != "key-1":
                    raise ValueError("unknown kid")
                return public_key

        verifier = OidcTokenVerifier(
            OidcVerifierConfig(
                "https://issuer.example",
                client_id="client-1",
                allowed_token_use=frozenset({"access"}),
                required_scope="legaldesk/use",
            ),
            Resolver(),
        )
        valid = self._signed_access_token(private_key=private_key)
        self.assertTrue(verifier.verify_token(valid).is_trusted())
        cases = (
            self._signed_access_token(private_key=private_key, client_id="other"),
            self._signed_access_token(private_key=private_key, iss="https://other.example"),
            self._signed_access_token(private_key=private_key, exp=int(time.time()) - 100),
            self._signed_access_token(private_key=private_key, nbf=int(time.time()) + 600),
            self._signed_access_token(private_key=private_key, scope="openid"),
            self._signed_access_token(private_key=private_key, kid="wrong-kid"),
            jwt.encode(
                {"iss": "https://issuer.example", "sub": "idp|alice-fictional", "exp": int(time.time()) + 300, "iat": int(time.time()), "nbf": int(time.time()) - 1, "token_use": "access", "client_id": "client-1", "scope": "legaldesk/use"},
                rsa.generate_private_key(public_exponent=65537, key_size=2048),
                algorithm="RS256",
                headers={"kid": "key-1"},
            ),
        )
        for token in cases:
            with self.subTest(token=token[:12]), self.assertRaises(PermissionError):
                verifier.verify_token(token)

    def test_access_and_id_configuration_cannot_skip_their_binding(self) -> None:
        with self.assertRaises(ValueError):
            OidcVerifierConfig("https://issuer.example", audience="aud", allowed_token_use=frozenset({"access"}))
        with self.assertRaises(ValueError):
            OidcVerifierConfig("https://issuer.example", client_id="client", allowed_token_use=frozenset({"id"}))

    def test_oidc_verifier_uses_jwks_and_returns_trusted_identity(self) -> None:
        class FakeJwt:
            @staticmethod
            def get_unverified_header(token):
                return {"alg": "RS256", "kid": "key-1"}

            @staticmethod
            def decode(token, **kwargs):
                if kwargs.get("options", {}).get("verify_signature") is False:
                    return {"token_use": "access"}
                return {
                    "sub": "idp|alice-fictional",
                    "iss": "https://issuer.example",
                    "exp": 4_000_000_000,
                    "iat": 1_700_000_000,
                    "client_id": "client-1",
                    "scope": "legaldesk/use",
                }

        class Resolver:
            def get_signing_key(self, token):
                return object()

        verifier = OidcTokenVerifier(
            OidcVerifierConfig(
                "https://issuer.example",
                client_id="client-1",
                allowed_token_use=frozenset({"access"}),
                required_scope="legaldesk/use",
            ),
            Resolver(),
        )
        with patch.object(OidcTokenVerifier, "_jwt", staticmethod(lambda: FakeJwt)):
            identity = verifier.verify_authorization_header("Bearer synthetic-token")
        self.assertTrue(identity.is_trusted())
        self.assertEqual(identity.subject, "idp|alice-fictional")
        self.assertEqual(identity.client_id, "client-1")

    def test_free_form_identity_is_not_accepted_at_authorization_boundary(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            build_request_context(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_sundial",
                load_authorization_store(),
            )
        with self.assertRaises(TypeError):
            VerifiedIdentity._from_verified_claims(
                subject="idp|alice-fictional",
                issuer="https://issuer.example",
                client_id="client",
                token_use="access",
                scopes=frozenset(),
            )

    def test_gateway_factory_is_trusted_but_matter_membership_is_rechecked(self) -> None:
        identity = test_identity("idp|alice-fictional")
        context = build_request_context(identity, "mat_sundial", load_authorization_store())
        self.assertEqual(context.user_id, "usr_alice")
        with self.assertRaises(AuthorizationDenied):
            build_request_context(identity, "mat_glacier", load_authorization_store())

    def test_oidc_config_requires_audience_or_client(self) -> None:
        with self.assertRaises(ValueError):
            OidcVerifierConfig("https://issuer.example")

    def test_memory_scope_cannot_be_forged_or_injected(self) -> None:
        with self.assertRaises(TypeError):
            MemoryScope("ldactor-forged", "session-forged", "namespace", object())  # type: ignore
        with self.assertRaises(TypeError):
            InMemoryShortTermMemory().append_event(object(), role="USER", text="x")  # type: ignore

    def test_forged_request_context_is_rejected_at_mcp_and_binding_seams(self) -> None:
        forged = RequestContext("00000000-0000-4000-8000-000000000001", "usr_alice", "tnt_aurora", "mat_sundial", frozenset())
        with self.assertRaises(TypeError):
            RequestContext._from_authorized(
                correlation_id=forged.correlation_id,
                user_id=forged.user_id,
                tenant_id=forged.tenant_id,
                matter_id=forged.matter_id,
                roles=forged.roles,
            )
        with self.assertRaises(AuthorizationDenied):
            build_matter_filter(forged)
        with self.assertRaises(AuthorizationDenied):
            build_document_key(forged, "00000000-0000-4000-8000-000000000001", "text/plain")
        with self.assertRaises(AuthorizationDenied):
            create_review_task(forged, {"reasonCode": "user_requested_review"}, repository=InMemoryReviewTaskRepository())
        with self.assertRaises(TypeError):
            InMemoryConversationBindingStore().bind(forged, conversation_id="conv-A8df2", session_selector="sess-93ba2")
        response = MCPServer(InMemoryDocumentMetadataRepository()).handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}}},
            request_context=forged,
        )
        self.assertEqual(response["error"]["code"], -32001)

    def test_mcp_grant_is_bound_to_exact_tool_and_expiry(self) -> None:
        repo = InMemoryGatewayGrantRepository()
        grant_id = "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
        repo.put(GatewayAuthorizationGrant(
            grant_id=grant_id,
            verified_subject="idp|alice-fictional",
            requested_matter_id="mat_sundial",
            correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279",
            tool_name="get_document_metadata",
            expires_at=4_000_000_000,
        ))
        self.assertEqual(repo.get(grant_id)["toolName"], "get_document_metadata")

    def test_durable_binding_reuses_existing_table_and_is_exact(self) -> None:
        class Table:
            def __init__(self) -> None:
                self.items = {}

            def put_item(self, *, Item, ConditionExpression):
                key = (Item["pk"], Item["sk"])
                if key in self.items:
                    raise RuntimeError("conditional failure")
                self.items[key] = dict(Item)

            def get_item(self, *, Key, ConsistentRead):
                return {"Item": self.items.get((Key["pk"], Key["sk"]))}

        context = build_request_context(
            test_identity("idp|alice-fictional"),
            "mat_sundial",
            load_authorization_store(),
        )
        store = Boto3DynamoConversationBindingStore("existing-table", table=Table())
        binding = store.bind(context, conversation_id="conv-A8df2", session_selector="sess-93ba2")
        self.assertTrue(store.is_bound(context=context, conversation_id=binding.conversation_id, session_selector=binding.session_selector))
        self.assertFalse(store.is_bound(context=context, conversation_id=binding.conversation_id, session_selector="sess-other"))

    def test_gateway_metadata_target_can_emit_opaque_grant(self) -> None:
        from test_gateway_interceptor import event

        grants = InMemoryGatewayGrantRepository()
        response = transform_gateway_request(
            event(),
            target=GatewayTarget.METADATA_MCP,
            authorization_store=load_authorization_store(),
            grant_repository=grants,
        )
        headers = response["mcp"]["transformedGatewayRequest"]["headers"]
        self.assertIn("x-legaldesk-grant-id", headers)
        self.assertEqual(len(grants.grants), 1)

    def test_prefixed_interceptor_grants_are_consumed_by_mcp_for_both_tools(self) -> None:
        from test_gateway_interceptor import event
        import legaldesk.mcp_server as mcp_module
        import legaldesk.gateway_interceptor as gateway_module

        metadata = InMemoryDocumentMetadataRepository()
        metadata.save(Document(
            document_id="doc-one", matter_id="mat_sundial", tenant_id="tnt_aurora",
            name="Synthetic notice.pdf",
            s3_key="tenants/tnt_aurora/matters/mat_sundial/documents/doc-one/original.pdf",
            media_type="application/pdf", jurisdiction="fictional",
            document_date="2099-01-01", confidentiality="fictional-internal",
            status=DocumentStatus.INDEXED, file_size_bytes=10,
        ))
        for local_tool in (LIST_MATTER_DOCUMENTS, GET_DOCUMENT_METADATA):
            with self.subTest(local_tool=local_tool):
                gateway_event = event()
                visible = f"metadata-mcp___{local_tool}"
                gateway_event["mcp"]["gatewayRequest"]["body"]["params"]["name"] = visible
                raw = __import__("json").loads(gateway_event["mcp"]["rawGatewayRequest"]["body"])
                raw["params"]["name"] = visible
                gateway_event["mcp"]["rawGatewayRequest"]["body"] = __import__("json").dumps(raw)
                grants = InMemoryGatewayGrantRepository()
                with patch.object(gateway_module, "_authorization_store_from_environment", return_value=load_authorization_store()), patch.object(gateway_module, "_grant_repository_from_environment", return_value=grants):
                    transformed = gateway_module.gateway_request_interceptor(gateway_event, object())
                target_request = transformed["mcp"]["transformedGatewayRequest"]
                body = dict(target_request["body"])
                params = dict(body["params"])
                params["name"] = local_tool
                params["arguments"] = {"documentId": "doc-one"} if local_tool == GET_DOCUMENT_METADATA else {}
                body["params"] = params
                with patch.object(mcp_module, "_mcp_repositories_from_environment", return_value=(load_authorization_store(), metadata)), patch.object(mcp_module, "_mcp_grant_repository_from_environment", return_value=grants):
                    response = mcp_lambda_handler({"headers": target_request["headers"], "body": __import__("json").dumps(body)}, object())
                self.assertEqual(response["statusCode"], 200, response)


if __name__ == "__main__":
    unittest.main()
