import base64
import json
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from legaldesk.authorization import InMemoryAuthorizationStore, build_request_context, _gateway_identity_from_verified_subject
from legaldesk.domain.models import Matter, User
from legaldesk.gateway_interceptor import (
    IDP_REVIEW_PURPOSE,
    IDP_REVIEW_SCOPE,
    InMemoryGatewayGrantRepository,
    transform_idp_review_gateway_request,
    IDPReviewInvocationRecord,
)
from legaldesk.idp.models import (
    DocumentType,
    EvidenceAnchor,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPExtractionRun,
    IDPFieldResult,
    IDPJobStatus,
)
from legaldesk.idp.review import (
    IDPDecisionAction,
    IDPReviewInvocation,
    IDPReviewService,
    IDPReviewError,
    InMemoryIDPDecisionRepository,
    query_selected_document,
    dispatch_review_after_persist,
)


HASH = "a" * 64


def _token(*, client="idp-client", scope=IDP_REVIEW_SCOPE, token_use="access"):
    payload = {"sub": "idp-service", "client_id": client, "scope": scope, "token_use": token_use}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{encoded}.x"


def _event(token, *, field_names=None):
    correlation = str(uuid4())
    args = {"matterId": "matter-a", "invocationId": "00000000-0000-0000-0000-000000000001"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "review-task-lambda___create_review_task", "arguments": args}}
    return {"mcp": {"gatewayRequest": {"headers": {"authorization": f"Bearer {token}"}, "body": body}, "rawGatewayRequest": {"body": body}}}


class ReviewTests(unittest.TestCase):
    def _run(self):
        field = IDPFieldResult(field="costs_statement", value="reserved", presence=FieldPresence.PRESENT, origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.REVIEW_REQUIRED, evidence=(EvidenceAnchor(page=1, quote="costs reserved", content_sha256=HASH),))
        return IDPExtractionRun(run_id="run-a", tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a", document_sha256=HASH, document_type=DocumentType.CONTRACT, schema_version="1.0.0", model_id="model", prompt_version="prompt", status=IDPJobStatus.REVIEW_REQUIRED, fields={field.field: field}, created_at=datetime.now(timezone.utc))

    def test_machine_gateway_grant_is_scoped_and_purpose_bound(self):
        grants = InMemoryGatewayGrantRepository()
        record = IDPReviewInvocationRecord("00000000-0000-0000-0000-000000000001", "idp-client", IDP_REVIEW_SCOPE, IDP_REVIEW_PURPOSE, "create_review_task", "matter-a", "doc-a", "run-a", HASH, str(uuid4()), ("costs_statement",), 4102444800)
        grants.put_idp_review_invocation(record)
        invocation = {"matter_id": "matter-a", "document_id": "doc-a", "run_id": "run-a", "document_sha256": HASH}
        response = transform_idp_review_gateway_request(_event(_token()), grant_repository=grants, machine_client_id="idp-client", invocation_resolver=lambda *_: invocation)
        grant_id = response["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]["_legaldeskGrantId"]
        raw = grants.get_idp_review(grant_id)
        self.assertEqual(raw["entityType"], "IDPReviewInvocationGrant")
        self.assertEqual(raw["purpose"], IDP_REVIEW_PURPOSE)
        with self.assertRaises(Exception):
            transform_idp_review_gateway_request(_event(_token(scope="legaldesk/use")), grant_repository=grants, machine_client_id="idp-client", invocation_resolver=lambda *_: invocation)

    def test_human_decision_is_append_only_and_query_is_idp_first(self):
        run = self._run()
        store = InMemoryAuthorizationStore(
            users_by_subject={"subject": User("user-a", "subject", frozenset({"tenant-a"}))},
            matters_by_id={"matter-a": Matter("matter-a", "tenant-a", "Matter", frozenset({"user-a"}))},
        )
        context = build_request_context(_gateway_identity_from_verified_subject("subject"), "matter-a", store, correlation_id=str(uuid4()))
        decisions = InMemoryIDPDecisionRepository()
        service = IDPReviewService(decisions)
        decision = service.record_human_decision(context=context, run=run, field_name="costs_statement", action=IDPDecisionAction.CORRECT, proposed={"value": "paid"}, result={"value": "paid", "presence": "PRESENT"}, evidence=run.fields["costs_statement"].evidence, reason="reviewed")
        self.assertEqual(service.effective_field(run=run, field_name="costs_statement")["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
        class Repo:
            def list_runs(self, **kwargs):
                return (run,)
        result = query_selected_document(context=context, document_id="doc-a", field_name="costs_statement", repository=Repo(), review_service=service)
        self.assertEqual(result.source, "IDP")
        self.assertEqual(result.value["value"], "paid")

    def test_dispatch_persists_invocation_before_gateway_and_marks_sent(self):
        from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository
        from legaldesk.idp.models import IDPJob
        class Gateway:
            def __init__(self, repo): self.repo = repo
            def dispatch(self, record):
                self.asserted = self.repo.get_idp_review_invocation(record.invocation_id)
                return {"reviewTaskId": "review-idp"}
        repo = InMemoryGatewayGrantRepository()
        job = IDPJob(job_id="run-a", tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a", document_sha256=HASH, idempotency_key="idem", correlation_id=str(uuid4()), model_id="model", prompt_version="prompt")
        gateway = Gateway(repo)
        result = dispatch_review_after_persist(run=self._run(), job=job, invocation_repository=repo, gateway=gateway, machine_client_id="idp-client")
        self.assertEqual(result["reviewTaskId"], "review-idp")
        self.assertEqual(repo.get_idp_review_invocation(next(iter(repo.idp_invocations)))["deliveryState"], "SENT")


if __name__ == "__main__":
    unittest.main()
