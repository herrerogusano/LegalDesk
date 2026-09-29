from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.authorization import InMemoryAuthorizationStore
from legaldesk.data_governance import (
    GovernanceAuthorizationError,
    GovernanceError,
    GovernanceLimitError,
    OperatorDataGovernanceService,
    authorize_operator_scope,
)
from legaldesk.documents import InMemoryDocumentMetadataRepository, InMemoryObjectStorage
from legaldesk.domain.models import Document, DocumentStatus, Matter, ReviewTask, ReviewTaskStatus, User
from legaldesk.review_tasks import InMemoryReviewTaskRepository
from legaldesk.state import InMemoryEphemeralStateStore
from fixture_loader import test_identity


TENANT = "tnt_fictional"
MATTER = "mat_public_beta"
SUBJECT = "idp|operator-fictional"
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


def operator_store() -> InMemoryAuthorizationStore:
    return InMemoryAuthorizationStore(
        users_by_subject={SUBJECT: User("usr_operator", SUBJECT, frozenset({TENANT}), frozenset({"operator"}))},
        matters_by_id={MATTER: Matter(MATTER, TENANT, "Fictional beta", frozenset({"usr_operator"}))},
    )


def document(document_id: str | None = None, *, matter_id: str = MATTER) -> Document:
    document_id = document_id or str(uuid4())
    key = f"tenants/{TENANT}/matters/{matter_id}/documents/{document_id}/original.txt"
    return Document(
        document_id=document_id,
        matter_id=matter_id,
        tenant_id=TENANT,
        name="fictional.txt",
        s3_key=key,
        quarantine_s3_key=f"quarantine/{key}",
        media_type="text/plain",
        jurisdiction="fictional",
        document_date="2099-01-01",
        confidentiality="public-fictional",
        status=DocumentStatus.UPLOADED,
        file_size_bytes=8,
    )


class FailOnceStorage(InMemoryObjectStorage):
    def __init__(self) -> None:
        super().__init__()
        self.failed = True

    def delete_object(self, *, key: str) -> None:
        if self.failed:
            self.failed = False
            raise RuntimeError("transient storage failure")
        super().delete_object(key=key)


class DataGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = InMemoryDocumentMetadataRepository()
        self.storage = InMemoryObjectStorage()
        self.reviews = InMemoryReviewTaskRepository()
        self.state = InMemoryEphemeralStateStore()
        self.service = OperatorDataGovernanceService(
            metadata_repository=self.metadata,
            object_storage=self.storage,
            review_repository=self.reviews,
            state_store=self.state,
            max_records=10,
            clock=lambda: NOW,
        )
        self.scope = authorize_operator_scope(test_identity(SUBJECT), MATTER, operator_store())

    def add_document(self, doc: Document) -> None:
        self.metadata.save(doc)
        self.storage.put_object(key=doc.s3_key, body=b"fictional", media_type=doc.media_type, metadata={})
        self.storage.put_object(key=f"{doc.s3_key}.metadata.json", body=b"{}", media_type="application/json", metadata={})
        self.storage.put_object(key=doc.quarantine_s3_key, body=b"fictional", media_type=doc.media_type, metadata={})

    def test_operator_export_is_bounded_metadata_only(self) -> None:
        doc = document()
        self.add_document(doc)
        task = ReviewTask(
            "review-1", MATTER, TENANT, "usr_operator", "user_requested_review",
            status=ReviewTaskStatus.CLOSED, snapshot={"answer": "private body"}, note="private note",
            correlation_id=str(uuid4()), created_at=NOW, updated_at=NOW,
            closed_at=NOW - timedelta(days=30), resolution_note="private resolution",
        )
        self.reviews.save(task)
        exported = self.service.export_matter_metadata(self.scope)
        encoded = json.dumps(exported)
        self.assertNotIn("private body", encoded)
        self.assertNotIn("private note", encoded)
        self.assertNotIn(doc.s3_key, encoded)
        self.assertEqual(exported["documents"][0]["documentId"], doc.document_id)
        with self.assertRaises(GovernanceError):
            self.service.export_matter_metadata(self.scope, include_approved_user_data=True)

    def test_cross_matter_operator_scope_denied_before_access(self) -> None:
        with self.assertRaises(GovernanceAuthorizationError):
            authorize_operator_scope(test_identity(SUBJECT), "mat_other", operator_store())

    def test_document_delete_cleans_canonical_quarantine_sidecars_and_is_idempotent(self) -> None:
        doc = document()
        self.add_document(doc)
        report = self.service.delete_document(self.scope, doc.document_id)
        self.assertTrue(report.ok, report.as_dict())
        self.assertIsNone(self.metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=doc.document_id))
        self.assertEqual(self.storage.objects, {})
        self.assertTrue(self.service.delete_document(self.scope, doc.document_id).ok)

    def test_delete_retries_after_partial_storage_failure_without_losing_metadata(self) -> None:
        storage = FailOnceStorage()
        self.service = OperatorDataGovernanceService(
            metadata_repository=self.metadata, object_storage=storage,
            review_repository=self.reviews, max_records=10, clock=lambda: NOW,
        )
        doc = document()
        self.metadata.save(doc)
        storage.put_object(key=doc.s3_key, body=b"fictional", media_type=doc.media_type, metadata={})
        first = self.service.delete_document(self.scope, doc.document_id)
        self.assertFalse(first.ok)
        stored = self.metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=doc.document_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, DocumentStatus.FAILED)
        self.assertTrue(first.index_cleanup_pending)
        second = self.service.delete_document(self.scope, doc.document_id)
        self.assertTrue(second.ok)

    def test_matter_delete_is_bounded_and_cross_scope_objects_remain(self) -> None:
        for _ in range(2):
            self.add_document(document())
        bounded = OperatorDataGovernanceService(
            metadata_repository=self.metadata, object_storage=self.storage,
            review_repository=self.reviews, max_records=1,
        )
        with self.assertRaises(GovernanceLimitError):
            bounded.delete_matter(self.scope)
        self.assertEqual(len(self.metadata.documents), 2)

    def test_matter_delete_removes_known_scoped_state_without_scan(self) -> None:
        history_key = (SUBJECT, TENANT, MATTER)
        self.state.add_history_event(history_key, "history-1")
        self.state.put_review_candidate(history_key, (NOW.timestamp() + 300, {"safe": "metadata"}))
        report = self.service.delete_matter(self.scope)
        self.assertTrue(report.ok, report.as_dict())
        self.assertGreaterEqual(report.state_records_removed, 2)
        self.assertEqual(self.state.list_history_ids(history_key), ())
        self.assertIsNone(self.state.get_review_candidate(history_key))

    def test_state_delete_does_not_overdelete_same_matter_id_in_other_tenant(self) -> None:
        self.state.append_audit({"subject": SUBJECT, "tenantId": "other-tenant", "matterId": MATTER, "operation": "keep"})
        self.state.append_audit({"subject": SUBJECT, "matterId": MATTER, "operation": "legacy-residual"})
        self.state.append_audit({"subject": SUBJECT, "tenantId": TENANT, "matterId": MATTER, "operation": "remove"})
        self.service.delete_matter(self.scope)
        events = self.state.list_audit(SUBJECT)
        self.assertEqual({item.get("operation") for item in events}, {"keep", "legacy-residual"})

    def test_export_rejects_review_boundary_without_overflow_cursor(self) -> None:
        for index in range(10):
            self.reviews.save(ReviewTask(
                f"review-{index}", MATTER, TENANT, "usr_operator", "user_requested_review",
                correlation_id=str(uuid4()), created_at=NOW, updated_at=NOW,
            ))
        with self.assertRaises(GovernanceLimitError):
            self.service.export_matter_metadata(self.scope)

    def test_closed_review_archive_strips_user_text_and_is_idempotent(self) -> None:
        task = ReviewTask(
            "review-1", MATTER, TENANT, "usr_operator", "user_requested_review",
            status=ReviewTaskStatus.CLOSED, snapshot={"passage": "body"}, note="note",
            correlation_id=str(uuid4()), created_at=NOW, updated_at=NOW,
            closed_at=NOW - timedelta(days=40), resolution_note="resolution",
        )
        self.reviews.save(task)
        report = self.service.archive_closed_reviews(self.scope, older_than=NOW - timedelta(days=30))
        self.assertTrue(report.ok)
        archived = self.reviews.get(context=self.scope.context, review_task_id="review-1")
        self.assertIsNotNone(archived)
        assert archived is not None
        self.assertIsNone(archived.snapshot)
        self.assertEqual(archived.note, "")
        self.assertIsNotNone(archived.archived_at)
        again = self.service.archive_closed_reviews(self.scope, older_than=NOW - timedelta(days=30))
        self.assertEqual(again.completed, 0)


if __name__ == "__main__":
    unittest.main()
