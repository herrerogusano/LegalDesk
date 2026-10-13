from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import load_authorization_store, test_identity  # noqa: E402
from legaldesk.agent_integration import bind_harness_invocation  # noqa: E402
from legaldesk.authorization import build_request_context  # noqa: E402
from legaldesk.gateway_client import (  # noqa: E402
    DirectGatewayInvoker,
    GatewayHttpResponse,
    GatewayInvocationError,
)
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository  # noqa: E402
from legaldesk.memory import InMemoryConversationBindingStore  # noqa: E402
from legaldesk.review_tasks import (  # noqa: E402
    AuthorizedToolEnvelope,
    ReviewTaskLambdaHandler,
    _decode_gateway_idp_decision_transport,
)


def _token(subject: str) -> str:
    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'sub': subject})}.unsigned"


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        request = json.loads(body)
        return GatewayHttpResponse(
            200,
            "application/json",
            json.dumps({
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]},
            }).encode(),
        )


def _review_binding():
    auth = load_authorization_store()
    identity = test_identity("idp|alice-fictional")
    binding_store = InMemoryConversationBindingStore()
    binding_store.bind(
        build_request_context(identity, "mat_sundial", auth),
        conversation_id="conversation-idp-transport",
        session_selector="session-idp-transport",
    )
    token = _token(identity.subject)

    class _Verifier:
        def verify_authorization_header(self, value: object) -> object:
            if value != f"Bearer {token}":
                raise ValueError("invalid token")
            return identity

    return bind_harness_invocation(
        bearer_token=token,
        gateway_url="https://gateway.example.test/mcp",
        identity_verifier=_Verifier(),
        requested_matter_id="mat_sundial",
        conversation_id="conversation-idp-transport",
        session_selector="session-idp-transport",
        authorization_store=auth,
        conversation_store=binding_store,
        invocation_repository=InMemoryGatewayGrantRepository(),
        application_action="review",
    )


class GatewayIDPTransportTests(unittest.TestCase):
    def test_raw_correction_values_are_opaque_json_at_gateway(self) -> None:
        values = (1300, "2026-02-01", ["one", "two"], True)
        for value in values:
            with self.subTest(value=value):
                transport = _Transport()
                DirectGatewayInvoker(
                    "https://gateway.example.test/mcp", transport=transport
                ).invoke(
                    _review_binding(),
                    tool_name="update_review_task",
                    arguments={
                        "reviewTaskId": "task-1",
                        "status": "IN_REVIEW",
                        "idpDecision": {
                            "fieldName": "contract_date",
                            "action": "CORRECT",
                            "reason": "verified",
                            "evidence": [],
                            "proposedValue": value,
                        },
                    },
                    request_id="11111111-1111-4111-8111-111111111111",
                )
                request = json.loads(transport.calls[0]["body"])
                decision = request["params"]["arguments"]["idpDecision"]
                self.assertNotIn("proposedValue", decision)
                self.assertEqual(json.loads(decision["proposedValueJson"]), value)

    def test_transport_rejects_both_nan_and_oversized_values_before_network(self) -> None:
        for decision in (
            {"proposedValue": 1, "proposedValueJson": "1"},
            {"proposedValueJson": "1"},
            {"proposedValue": float("nan")},
            {"proposedValue": "x" * (16 * 1024)},
            {"action": "APPROVE", "proposedValue": None},
        ):
            transport = _Transport()
            with self.subTest(decision=decision), self.assertRaises(GatewayInvocationError):
                DirectGatewayInvoker(
                    "https://gateway.example.test/mcp", transport=transport
                ).invoke(
                    _review_binding(),
                    tool_name="update_review_task",
                    arguments={"idpDecision": decision},
                )
            self.assertEqual(transport.calls, [])

    def test_target_decodes_wire_value_and_rejects_ambiguous_json(self) -> None:
        raw = {
            "reviewTaskId": "task-1",
            "status": "IN_REVIEW",
            "idpDecision": {
                "fieldName": "amount",
                "action": "CORRECT",
                "reason": "verified",
                "evidence": [],
                "proposedValueJson": "[1,true,\"2026-02-01\"]",
            },
        }
        self.assertEqual(
            _decode_gateway_idp_decision_transport(raw)["idpDecision"]["proposedValue"],
            [1, True, "2026-02-01"],
        )
        with self.assertRaises(Exception):
            _decode_gateway_idp_decision_transport({
                "idpDecision": {
                    "fieldName": "amount",
                    "action": "CORRECT",
                    "reason": "verified",
                    "proposedValue": 1,
                    "proposedValueJson": "1",
                }
            })
        with self.assertRaises(Exception):
            _decode_gateway_idp_decision_transport({
                "idpDecision": {
                    "fieldName": "amount",
                    "action": "CORRECT",
                    "reason": "verified",
                    "proposedValueJson": '{"x":1,"x":2}',
                }
            })
        with self.assertRaises(Exception):
            _decode_gateway_idp_decision_transport({
                "idpDecision": {
                    "fieldName": "amount",
                    "action": "APPROVE",
                    "reason": "verified",
                    "proposedValueJson": "null",
                }
            })
        with self.assertRaises(Exception):
            _decode_gateway_idp_decision_transport({
                "idpDecision": {
                    "fieldName": "amount",
                    "action": "CORRECT",
                    "reason": "verified",
                    "proposedValueJson": "1e309",
                }
            })

    def test_lambda_target_decodes_before_existing_update_validator(self) -> None:
        auth = load_authorization_store()
        context = AuthorizedToolEnvelope(
            "idp|alice-fictional", "mat_sundial", "11111111-1111-4111-8111-111111111111"
        )
        handler = ReviewTaskLambdaHandler(None, auth)  # type: ignore[arg-type]
        with patch("legaldesk.review_tasks.update_review_task", return_value={"ok": True}) as update:
            result = handler.handle_operation(
                "update_review_task",
                {
                    "arguments": {
                        "reviewTaskId": "task-1",
                        "status": "IN_REVIEW",
                        "idpDecision": {
                            "fieldName": "amount",
                            "action": "CORRECT",
                            "reason": "verified",
                            "evidence": [],
                            "proposedValueJson": "1300",
                        },
                    }
                },
                authorized_context=context,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(update.call_args.args[1]["idpDecision"]["proposedValue"], 1300)
        self.assertNotIn("proposedValueJson", update.call_args.args[1]["idpDecision"])


if __name__ == "__main__":
    unittest.main()
