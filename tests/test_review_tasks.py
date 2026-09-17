from __future__ import annotations

import sys
import unittest
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store
from legaldesk.authorization import (
    AuthorizationDenied,
    Boto3DynamoAuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    authorization_matter_partition_key,
    authorization_profile_sort_key,
    authorization_user_partition_key,
)
from legaldesk.domain.models import ReviewTaskStatus
from legaldesk.domain.models import ReviewTask
from legaldesk.gateway_interceptor import (
    GatewayAuthorizationGrant,
    InMemoryGatewayGrantRepository,
)
from legaldesk.review_tasks import (
    CREATE_REVIEW_TASK_TOOL_SCHEMA,
    AuthorizedToolEnvelope,
    Boto3DynamoReviewTaskRepository,
    InMemoryReviewTaskRepository,
    ReviewTaskLambdaHandler,
    ReviewTaskIdempotencyConflict,
    ReviewTaskPersistenceError,
    ReviewTaskValidationError,
    create_review_task,
    create_review_task_for_identity,
    gateway_lambda_handler,
    lambda_handler,
    parse_review_task_input,
)
import legaldesk.review_tasks as review_tasks


ALICE = VerifiedIdentity("idp|alice-fictional")
CORRELATION_ID = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"


def context(matter_id: str = "mat_sundial") -> RequestContext:
    return RequestContext(
        correlation_id=CORRELATION_ID,
        user_id="usr_alice",
        tenant_id="tnt_aurora",
        matter_id=matter_id,
        roles=frozenset({"member"}),
    )


class ReviewTaskToolTests(unittest.TestCase):
    def test_schema_is_strict_and_has_minimal_input(self) -> None:
        schema = CREATE_REVIEW_TASK_TOOL_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["reasonCode"])
        self.assertEqual(set(schema["properties"]), {"reasonCode", "idempotencyKey"})
        with self.assertRaises(ReviewTaskValidationError):
            parse_review_task_input({"reasonCode": "user_requested_review", "matterId": "mat_glacier"})
        with self.assertRaises(ReviewTaskValidationError):
            parse_review_task_input({"reasonCode": "user_requested_review", "status": "closed"})

    def test_authorized_create_derives_scope_and_open_status(self) -> None:
        repository = InMemoryReviewTaskRepository()
        response = create_review_task(
            context(), {"reasonCode": "material_legal_judgment"}, repository=repository
        )
        self.assertEqual(set(response), {"reviewTaskId", "status"})
        self.assertEqual(response["status"], ReviewTaskStatus.OPEN.value)
        task = next(iter(repository.tasks.values()))
        self.assertEqual(task.tenant_id, "tnt_aurora")
        self.assertEqual(task.matter_id, "mat_sundial")
        self.assertEqual(task.created_by_user_id, "usr_alice")
        self.assertEqual(task.reason_code, "material_legal_judgment")
        self.assertEqual(task.correlation_id, CORRELATION_ID)

    def test_identity_adapter_rejects_unknown_or_cross_matter_before_persist(self) -> None:
        repository = InMemoryReviewTaskRepository()
        auth = load_authorization_store()
        with self.assertRaises(AuthorizationDenied):
            create_review_task_for_identity(
                ALICE,
                "mat_glacier",
                {"reasonCode": "user_requested_review"},
                authorization_store=auth,
                repository=repository,
            )
        with self.assertRaises(AuthorizationDenied):
            create_review_task_for_identity(
                ALICE,
                "mat_unknown",
                {"reasonCode": "user_requested_review"},
                authorization_store=auth,
                repository=repository,
            )
        self.assertEqual(repository.tasks, {})

    def test_handler_authorizes_before_any_repository_operation(self) -> None:
        class CountingRepository:
            def __init__(self) -> None:
                self.get_calls = 0
                self.save_calls = 0

            def get(self, **_: object) -> None:
                self.get_calls += 1
                return None

            def save(self, _: ReviewTask) -> None:
                self.save_calls += 1

        repository = CountingRepository()
        response = ReviewTaskLambdaHandler(repository, load_authorization_store()).handle(
            {"arguments": {"reasonCode": "user_requested_review"}},
            authorized_context=AuthorizedToolEnvelope(
                ALICE.subject, "mat_glacier", CORRELATION_ID
            ),
        )
        self.assertEqual(response, {"error": "access_denied"})
        self.assertEqual((repository.get_calls, repository.save_calls), (0, 0))

    def test_malformed_reason_and_client_overrides_are_rejected(self) -> None:
        repository = InMemoryReviewTaskRepository()
        for payload in (
            {},
            {"reason": ""},
            {"reasonCode": "not_a_real_code"},
            {"reasonCode": "user_requested_review", "tenantId": "tnt_borealis"},
            {"reasonCode": "user_requested_review", "matterId": "mat_glacier"},
            {"reasonCode": "user_requested_review", "userId": "usr_bob"},
            {"reasonCode": "user_requested_review", "taskId": "review-other"},
            {"reasonCode": "user_requested_review", "status": "closed"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ReviewTaskValidationError):
                create_review_task(context(), payload, repository=repository)

    def test_idempotency_is_scoped_and_conflicts_are_safe(self) -> None:
        repository = InMemoryReviewTaskRepository()
        first = create_review_task(
            context(), {"reasonCode": "user_requested_review", "idempotencyKey": "retry-1"}, repository=repository
        )
        retry = create_review_task(
            context(), {"reasonCode": "user_requested_review", "idempotencyKey": "retry-1"}, repository=repository
        )
        self.assertEqual(first, retry)
        self.assertEqual(len(repository.tasks), 1)
        with self.assertRaises(ReviewTaskIdempotencyConflict) as raised:
            create_review_task(
                context(),
                {"reasonCode": "safety_escalation", "idempotencyKey": "retry-1"},
                repository=repository,
            )
        self.assertEqual(str(raised.exception), "idempotency key was already used")
        other_scope = create_review_task(
            context("mat_other"),
            {"reasonCode": "user_requested_review", "idempotencyKey": "retry-1"},
            repository=repository,
        )
        self.assertNotEqual(first["reviewTaskId"], other_scope["reviewTaskId"])

    def test_handler_uses_injected_context_and_returns_no_advice(self) -> None:
        repository = InMemoryReviewTaskRepository()
        handler = ReviewTaskLambdaHandler(repository, load_authorization_store())
        response = handler.handle(
            {
                "arguments": {"reasonCode": "material_legal_judgment"},
                "authorizedContext": {
                    "verifiedSubject": ALICE.subject,
                    "requestedMatterId": "mat_sundial",
                    "correlationId": CORRELATION_ID,
                },
            },
            authorized_context=AuthorizedToolEnvelope(
                ALICE.subject, "mat_sundial", CORRELATION_ID
            ),
        )
        self.assertEqual(set(response), {"reviewTaskId", "status"})
        self.assertNotIn("reason", response)
        self.assertEqual(
            handler.handle(
                {
                    "arguments": {
                        "reasonCode": "user_requested_review",
                        "matterId": "mat_glacier",
                    }
                },
                authorized_context=AuthorizedToolEnvelope(
                    ALICE.subject, "mat_sundial", CORRELATION_ID
                ),
            ),
            {"error": "invalid_request"},
        )
        self.assertEqual(handler.handle({}), {"error": "access_denied"})

    def test_envelope_rejects_non_opaque_selectors_and_non_uuid_correlation(self) -> None:
        with self.assertRaises(ValueError):
            AuthorizedToolEnvelope("idp|alice-fictional", "../mat_sundial", CORRELATION_ID)
        with self.assertRaises(ValueError):
            AuthorizedToolEnvelope("idp|alice fictional", "mat_sundial", CORRELATION_ID)
        with self.assertRaises(ValueError):
            AuthorizedToolEnvelope("idp|alice-fictional", "mat_sundial", "not-a-uuid")

    def test_recovered_idempotent_object_must_match_every_scope_field(self) -> None:
        task_id = review_tasks._idempotent_task_id(context(), "race-key")
        expected = ReviewTask(
            review_task_id=task_id,
            matter_id=context().matter_id,
            tenant_id=context().tenant_id,
            created_by_user_id=context().user_id,
            reason="user_requested_review",
            correlation_id=CORRELATION_ID,
        )

        class RaceRepository:
            def __init__(self, recovered: ReviewTask) -> None:
                self.recovered = recovered
                self.get_calls = 0

            def get(self, **_: object) -> ReviewTask | None:
                self.get_calls += 1
                return None if self.get_calls == 1 else self.recovered

            def save(self, _: ReviewTask) -> None:
                raise RuntimeError("conditional write lost")

        for field, value in (
            ("tenant_id", "tnt_other"),
            ("matter_id", "mat_other"),
            ("created_by_user_id", "usr_other"),
        ):
            with self.subTest(field=field):
                recovered = ReviewTask(
                    review_task_id=expected.review_task_id,
                    matter_id=value if field == "matter_id" else expected.matter_id,
                    tenant_id=value if field == "tenant_id" else expected.tenant_id,
                    created_by_user_id=value if field == "created_by_user_id" else expected.created_by_user_id,
                    reason=expected.reason,
                    correlation_id=expected.correlation_id,
                )
                with self.assertRaises(ReviewTaskIdempotencyConflict):
                    create_review_task(
                        context(),
                        {"reasonCode": expected.reason, "idempotencyKey": "race-key"},
                        repository=RaceRepository(recovered),
                    )

    def test_malformed_recovered_object_fails_closed(self) -> None:
        class BrokenRepository:
            def get(self, **_: object) -> object:
                return object()

            def save(self, _: ReviewTask) -> None:
                raise AssertionError("not reached")

        with self.assertRaises(ReviewTaskPersistenceError):
            create_review_task(
                context(),
                {"reasonCode": "user_requested_review", "idempotencyKey": "bad-record"},
                repository=BrokenRepository(),
            )

    def test_dynamo_rejects_malformed_task_and_wrong_id(self) -> None:
        class FakeTable:
            def __init__(self, item: dict[str, object]) -> None:
                self.item = item

            def get_item(self, **_: object) -> dict[str, object]:
                return {"Item": self.item}

        valid = {
            "pk": "TENANT#tnt_aurora#MATTER#mat_sundial",
            "sk": "REVIEW#review-expected",
            "entityType": "ReviewTask",
            "tenantId": "tnt_aurora",
            "matterId": "mat_sundial",
            "reviewTaskId": "review-expected",
            "createdByUserId": "usr_alice",
            "reason": "user_requested_review",
            "status": "open",
            "correlationId": CORRELATION_ID,
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        }
        for field, value in (("reason", "free text"), ("reviewTaskId", "review-other")):
            with self.subTest(field=field):
                item = {**valid, field: value}
                with self.assertRaises(ReviewTaskPersistenceError):
                    Boto3DynamoReviewTaskRepository("table", table=FakeTable(item)).get(
                        context=context(), review_task_id="review-expected"
                    )

    def test_persistence_failure_returns_safe_handler_error(self) -> None:
        class BrokenRepository:
            def get(self, **_: Any) -> None:
                raise RuntimeError("secret provider details")

            def save(self, task: object) -> None:
                raise AssertionError("not reached")

        response = ReviewTaskLambdaHandler(
            BrokenRepository(), load_authorization_store()
        ).handle(
            {"arguments": {"reasonCode": "user_requested_review"}},
            authorized_context=AuthorizedToolEnvelope(
                ALICE.subject, "mat_sundial", CORRELATION_ID
            ),
        )
        self.assertEqual(response, {"error": "service_unavailable"})
        self.assertNotIn("secret", str(response))

    def test_aws_entrypoint_is_two_argument_and_fails_closed_without_gateway_scope(self) -> None:
        response = lambda_handler({"reasonCode": "user_requested_review"}, object())
        self.assertEqual(response, {"error": "invalid_request"})

    def test_entrypoint_fails_closed_on_unknown_schema_version(self) -> None:
        event = {
            "arguments": {"reasonCode": "user_requested_review"},
            "authorizedContext": {
                "verifiedSubject": ALICE.subject,
                "requestedMatterId": "mat_sundial",
                "correlationId": CORRELATION_ID,
            },
        }
        with patch.dict(
            os.environ,
            {"REVIEW_TASK_TABLE_NAME": "fictional-table", "REVIEW_TASK_SCHEMA_VERSION": "999"},
        ):
            self.assertEqual(lambda_handler(event, object()), {"error": "service_unavailable"})

    def test_entrypoint_reauthorizes_serializable_envelope_before_create(self) -> None:
        repository = InMemoryReviewTaskRepository()
        event = {
            "arguments": {
                "reasonCode": "insufficient_evidence",
                "idempotencyKey": "entrypoint-retry",
            },
            "authorizedContext": {
                "verifiedSubject": ALICE.subject,
                "requestedMatterId": "mat_sundial",
                "correlationId": CORRELATION_ID,
            },
        }
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ):
            response = lambda_handler(event, object())
        self.assertEqual(response["status"], "open")
        self.assertEqual(len(repository.tasks), 1)

    def test_entrypoint_cross_matter_and_forged_scope_never_write(self) -> None:
        repository = InMemoryReviewTaskRepository()
        base = {
            "arguments": {"reasonCode": "user_requested_review"},
            "authorizedContext": {
                "verifiedSubject": ALICE.subject,
                "requestedMatterId": "mat_glacier",
                "correlationId": CORRELATION_ID,
            },
        }
        forged = {
            **base,
            "authorizedContext": {**base["authorizedContext"], "tenantId": "tnt_aurora"},
        }
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ):
            self.assertEqual(lambda_handler(base, object()), {"error": "access_denied"})
            self.assertEqual(lambda_handler(forged, object()), {"error": "invalid_request"})
        self.assertEqual(repository.tasks, {})

    def test_gateway_entrypoint_accepts_flat_target_event_and_dynamo_grant(self) -> None:
        repository = InMemoryReviewTaskRepository()
        grants = InMemoryGatewayGrantRepository()
        grant = GatewayAuthorizationGrant(
            "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279",
            ALICE.subject,
            "mat_sundial",
            CORRELATION_ID,
            "create_review_task",
            int(time.time()) + 300,
        )
        grants.put(grant)
        event = {
            "reasonCode": "user_requested_review",
            "matterId": "mat_glacier",
            "idempotencyKey": "model-controlled-key",
            "_legaldeskGrantId": grant.grant_id,
        }

        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ), patch.object(
            review_tasks,
            "_gateway_grant_repository_from_environment",
            return_value=grants,
        ):
            response = gateway_lambda_handler(
                event,
                object(),
            )
        expected = create_review_task(
            context(),
            {
                "reasonCode": "user_requested_review",
                "idempotencyKey": f"gateway-{grant.grant_id}",
            },
            repository=InMemoryReviewTaskRepository(),
        )
        self.assertEqual(response["status"], "open")
        self.assertEqual(response["reviewTaskId"], expected["reviewTaskId"])
        self.assertEqual(len(repository.tasks), 1)
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ), patch.object(
            review_tasks,
            "_gateway_grant_repository_from_environment",
            return_value=grants,
        ):
            retry = gateway_lambda_handler(event, object())
        self.assertEqual(retry["reviewTaskId"], response["reviewTaskId"])
        self.assertEqual(len(repository.tasks), 1)

        # The existing Phase 07 Lambda handler dispatches the same trusted
        # flat-target contract, so the ARN need not change for Phase 08.
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ), patch.object(
            review_tasks,
            "_gateway_grant_repository_from_environment",
            return_value=grants,
        ):
            response = lambda_handler(
                {"reasonCode": "user_requested_review", "_legaldeskGrantId": grant.grant_id},
                object(),
            )
        self.assertEqual(response["status"], "open")

    def test_gateway_entrypoint_discards_model_scope_and_requires_trusted_context(self) -> None:
        self.assertEqual(
            gateway_lambda_handler({"reasonCode": "user_requested_review"}, object()),
            {"error": "access_denied"},
        )
        repository = InMemoryReviewTaskRepository()
        grants = InMemoryGatewayGrantRepository()
        grant = GatewayAuthorizationGrant(
            "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd280",
            ALICE.subject,
            "mat_sundial",
            CORRELATION_ID,
            "create_review_task",
            int(time.time()) + 300,
        )
        grants.put(grant)
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ), patch.object(
            review_tasks,
            "_gateway_grant_repository_from_environment",
            return_value=grants,
        ):
            response = gateway_lambda_handler(
                {"reasonCode": "user_requested_review", "matterId": "mat_glacier", "_legaldeskGrantId": grant.grant_id},
                object(),
            )
        self.assertEqual(response["status"], "open")
        self.assertEqual(len(repository.tasks), 1)
        task = next(iter(repository.tasks.values()))
        self.assertEqual(task.matter_id, "mat_sundial")
        self.assertEqual(task.tenant_id, "tnt_aurora")

    def test_gateway_entrypoint_rejects_model_supplied_authorized_context(self) -> None:
        grants = InMemoryGatewayGrantRepository()
        grant = GatewayAuthorizationGrant(
            "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd281",
            ALICE.subject,
            "mat_sundial",
            CORRELATION_ID,
            "create_review_task",
            int(time.time()) + 300,
        )
        grants.put(grant)
        response = gateway_lambda_handler(
            {
                "reasonCode": "user_requested_review",
                "_legaldeskGrantId": grant.grant_id,
                "authorizedContext": {"verifiedSubject": ALICE.subject},
            },
            object(),
        )
        self.assertEqual(response, {"error": "access_denied"})

    def test_gateway_entrypoint_rejects_malformed_expired_and_wrong_tool_grants(self) -> None:
        repository = InMemoryReviewTaskRepository()
        grants = InMemoryGatewayGrantRepository()
        for grant_id, tool_name, expires_at in (
            ("9ec5d1c5-7b58-4bc2-a183-8fd48a3bd282", "create_review_task", int(time.time()) - 1),
            ("9ec5d1c5-7b58-4bc2-a183-8fd48a3bd283", "metadata-mcp", int(time.time()) + 300),
        ):
            grants.put(
                GatewayAuthorizationGrant(
                    grant_id, ALICE.subject, "mat_sundial", CORRELATION_ID,
                    tool_name, expires_at,
                )
            )
        with patch.object(
            review_tasks,
            "_repositories_from_environment",
            return_value=(repository, load_authorization_store()),
        ), patch.object(
            review_tasks,
            "_gateway_grant_repository_from_environment",
            return_value=grants,
        ):
            for grant_id in (
                "not-a-uuid",
                "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd282",
                "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd283",
            ):
                with self.subTest(grant_id=grant_id):
                    self.assertEqual(
                        gateway_lambda_handler(
                            {"reasonCode": "user_requested_review", "_legaldeskGrantId": grant_id},
                            object(),
                        )["error"],
                        "access_denied" if grant_id != "not-a-uuid" else "invalid_request",
                    )
        self.assertEqual(repository.tasks, {})

    def test_dynamo_authorization_adapter_reads_only_documented_auth_keys(self) -> None:
        class FakeTable:
            def __init__(self) -> None:
                self.items = {
                    (
                        authorization_user_partition_key(ALICE.subject),
                        authorization_profile_sort_key(),
                    ): {
                        "entityType": "User",
                        "userId": "usr_alice",
                        "verifiedSubject": ALICE.subject,
                        "tenantIds": ["tnt_aurora"],
                        "roles": ["member"],
                    },
                    (
                        authorization_matter_partition_key("mat_sundial"),
                        authorization_profile_sort_key(),
                    ): {
                        "entityType": "Matter",
                        "matterId": "mat_sundial",
                        "tenantId": "tnt_aurora",
                        "name": "Fictional matter",
                        "authorizedUserIds": ["usr_alice"],
                        "status": "active",
                    },
                }

            def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, object]:
                self.assert_consistent(ConsistentRead)
                return {"Item": self.items.get((Key["pk"], Key["sk"]))}

            @staticmethod
            def assert_consistent(value: bool) -> None:
                if not value:
                    raise AssertionError("authorization must use consistent reads")

        store = Boto3DynamoAuthorizationStore("existing-table", table=FakeTable())
        self.assertEqual(store.get_user_by_subject(ALICE.subject).user_id, "usr_alice")  # type: ignore[union-attr]
        self.assertEqual(store.get_matter("mat_sundial").tenant_id, "tnt_aurora")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
