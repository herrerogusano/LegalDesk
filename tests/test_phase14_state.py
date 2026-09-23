from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.authorization import VerifiedIdentity
from legaldesk.http_app import ApplicationComposition, LoopbackLegalDeskApp
from legaldesk.state import (
    CitationHandle,
    DynamoDBEphemeralStateStore,
    IngestionOperationRecord,
    InMemoryEphemeralStateStore,
    SessionRecord,
)


class RecordingDynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.history_expiry: object = 200.0

    def put_item(self, *, Item: dict[str, object], **kwargs: object) -> dict[str, object]:
        self.calls.append(("put_item", dict(kwargs)))
        key = (str(Item["pk"]), str(Item["sk"]))
        condition = str(kwargs.get("ConditionExpression", ""))
        if "attribute_not_exists" in condition and key in self.items:
            raise RuntimeError("conditional check failed")
        if "attribute_exists" in condition and key not in self.items:
            raise RuntimeError("conditional check failed")
        self.items[key] = dict(Item)
        return {}

    def get_item(self, *, Key: dict[str, str], **kwargs: object) -> dict[str, object]:
        self.calls.append(("get_item", dict(kwargs)))
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, *, Key: dict[str, str], **kwargs: object) -> dict[str, object]:
        self.calls.append(("delete_item", dict(kwargs)))
        key = (Key["pk"], Key["sk"])
        item = self.items.get(key)
        if kwargs.get("ConditionExpression") and item is None:
            raise RuntimeError("conditional check failed")
        if item is None:
            return {}
        del self.items[key]
        return {"Attributes": dict(item)} if kwargs.get("ReturnValues") == "ALL_OLD" else {}

    def update_item(self, *, Key: dict[str, str], **kwargs: object) -> dict[str, object]:
        self.calls.append(("update_item", dict(kwargs)))
        item = self.items[(Key["pk"], Key["sk"])]
        for name, value in kwargs.get("ExpressionAttributeValues", {}).items():
            if name == ":correlation":
                item["correlationId"] = value
        return {}

    def query(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("query", dict(kwargs)))
        names = kwargs.get("ExpressionAttributeNames", {})
        if "eventId" in names.values():
            return {"Items": [{"eventId": "event-1", "expiresAt": self.history_expiry}]}
        return {"Items": [{"operation": "chat"}]}

    def scan(self, **_kwargs: object) -> None:
        raise AssertionError("state store must never scan")


def _session() -> SessionRecord:
    return SessionRecord(VerifiedIdentity("alice"), "access-secret", "csrf", 10_000)


class Phase14StateTests(unittest.TestCase):
    def test_shared_store_survives_app_recreation(self) -> None:
        store = InMemoryEphemeralStateStore(clock=lambda: 100.0)
        composition = ApplicationComposition(
            identity_verifier=object(),
            token_exchange=None,
            authorization_store=object(),
            conversation_store=object(),
            document_pipeline=object(),
            object_storage=object(),
            metadata_repository=object(),
            mcp_server=object(),
            state_store=store,
            matter_catalog=("matter-a",),
        )
        first, second = LoopbackLegalDeskApp(composition), LoopbackLegalDeskApp(composition)
        first.state_store.put_session("selector", _session())
        self.assertEqual(second.state_store.get_session("selector").access_token, "access-secret")
        self.assertNotIn("access-secret", repr(second.state_store.get_session("selector")))

    def test_oauth_state_is_single_use_and_expiry_fails_closed(self) -> None:
        now = [100.0]
        store = InMemoryEphemeralStateStore(clock=lambda: now[0])
        store.put_oauth_state("once", "verifier", 200.0)
        self.assertEqual(store.consume_oauth_state("once"), ("verifier", 200.0))
        self.assertIsNone(store.consume_oauth_state("once"))
        store.put_oauth_state("expired", "verifier", 99.0)
        self.assertIsNone(store.consume_oauth_state("expired"))

    def test_citation_scope_rejects_cross_subject_and_matter(self) -> None:
        store = InMemoryEphemeralStateStore(clock=lambda: 100.0)
        citation = CitationHandle("h", "alice", "tenant-a", "matter-a", "conversation-a", "doc-a", "fictional passage", 200.0)
        store.put_citation(citation, citation_key=("alice", "conversation-a", "corr-a", "citation-a"))
        self.assertIsNone(store.get_citation("h", subject="bob"))
        self.assertIsNone(store.get_citation("h", subject="alice", matter_id="matter-b"))
        self.assertEqual(store.get_citation("h", subject="alice", matter_id="matter-a").passage, "fictional passage")

    def test_audit_store_allowlist_redacts_tokens_and_documents(self) -> None:
        store = InMemoryEphemeralStateStore()
        store.append_audit({
            "subject": "alice",
            "matterId": "matter-a",
            "operation": "chat",
            "access_token": "secret-token",
            "answer": "fictional answer body",
        })
        record = store.list_audit("alice")[0]
        self.assertEqual(record["operation"], "chat")
        self.assertNotIn("access_token", record)
        self.assertNotIn("answer", record)

    def test_dynamo_state_uses_bounded_queries_and_never_scan(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        store.put_oauth_state("state", "verifier", 200.0)
        self.assertEqual(store.consume_oauth_state("state"), ("verifier", 200.0))
        store.add_history_event(("alice", "conversation-a", "session-a"), "event-1")
        self.assertEqual(store.list_history_ids(("alice", "conversation-a", "session-a")), ("event-1",))
        store.append_audit({"subject": "alice", "matterId": "matter-a", "operation": "chat"})
        self.assertEqual(store.list_audit("alice")[0]["operation"], "chat")
        query_calls = [kwargs for name, kwargs in table.calls if name == "query"]
        self.assertEqual(len(query_calls), 2)
        self.assertTrue(all(0 < int(kwargs["Limit"]) <= 1_000 for kwargs in query_calls))
        self.assertTrue(any("ProjectionExpression" in kwargs for kwargs in query_calls))
        self.assertFalse(any(name == "scan" for name, _kwargs in table.calls))

    def test_dynamo_state_rejects_missing_or_malformed_expiry(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        store.bind_conversation("conversation-a", ("alice", "tenant-a", "matter-a", "selector-a"), "corr-a")
        conversation_key = (store._key("CONVERSATION", "conversation-a", "RECORD")["pk"], "RECORD")
        conversation_item = table.items[conversation_key]
        conversation_item.pop("expiresAt")
        self.assertIsNone(store.get_conversation("conversation-a"))
        conversation_item["expiresAt"] = "not-a-timestamp"
        self.assertIsNone(store.get_conversation_correlation("conversation-a"))

        store.add_history_event(("alice", "conversation-a", "selector-a"), "event-1")
        table.history_expiry = None
        self.assertEqual(store.list_history_ids(("alice", "conversation-a", "selector-a")), ())
        table.history_expiry = "not-a-timestamp"
        self.assertEqual(store.list_history_ids(("alice", "conversation-a", "selector-a")), ())

    def test_dynamo_session_identity_is_placeholder_and_http_reverifies_token(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        store.put_session("selector", SessionRecord(VerifiedIdentity("alice"), "stored-access-token", "csrf", 200.0))
        restored = store.get_session("selector")
        self.assertIsNotNone(restored)
        self.assertIsNone(restored.identity._trust)

        class Verifier:
            calls = 0

            def verify_authorization_header(self, header: str) -> VerifiedIdentity:
                self.calls += 1
                if header != "Bearer stored-access-token":
                    raise AssertionError(header)
                return VerifiedIdentity("alice")

        verifier = Verifier()
        composition = ApplicationComposition(
            identity_verifier=verifier,
            token_exchange=None,
            authorization_store=object(),
            conversation_store=object(),
            document_pipeline=object(),
            object_storage=object(),
            metadata_repository=object(),
            mcp_server=object(),
            state_store=store,
            matter_catalog=("matter-a",),
        )
        app = LoopbackLegalDeskApp(composition)
        _key, identity_session = app._session({"HTTP_COOKIE": "legaldesk_session=selector"})
        self.assertEqual(identity_session.identity.subject, "alice")
        self.assertEqual(verifier.calls, 1)

    def test_dynamo_ingestion_operation_requires_valid_expiry(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        operation = IngestionOperationRecord(
            operation_id="ing_opaque", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="STARTING", created_at=100.0, updated_at=100.0, expires_at=200.0,
        )
        store.put_ingestion_operation(operation)
        key = (store._key("INGESTION_OPERATION", operation.operation_id, "RECORD")["pk"], "RECORD")
        table.items[key].pop("expiresAt")
        self.assertIsNone(store.get_ingestion_operation(operation.operation_id))
        table.items[key]["expiresAt"] = "not-a-timestamp"
        self.assertIsNone(store.get_ingestion_operation(operation.operation_id))

    def test_dynamo_ingestion_operation_job_id_combination_is_fail_closed(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        operation = IngestionOperationRecord(
            operation_id="ing_starting", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="",
            status="STARTING", provider_status="STARTING", created_at=100.0, updated_at=100.0, expires_at=200.0,
        )
        store.put_ingestion_operation(operation)
        key = (store._key("INGESTION_OPERATION", operation.operation_id, "RECORD")["pk"], "RECORD")
        self.assertEqual(store.get_ingestion_operation(operation.operation_id).ingestion_job_id, "")
        table.items[key]["status"] = "PENDING"
        self.assertIsNone(store.get_ingestion_operation(operation.operation_id))
        table.items[key]["status"] = "STARTING"
        table.items[key]["providerStatus"] = "IN_PROGRESS"
        self.assertIsNone(store.get_ingestion_operation(operation.operation_id))
        table.items[key]["providerStatus"] = "STARTING"
        table.items[key]["ingestionJobId"] = "   "
        self.assertIsNone(store.get_ingestion_operation(operation.operation_id))

    def test_active_ingestion_document_set_is_a_bounded_point_binding(self) -> None:
        table = RecordingDynamoTable()
        store = DynamoDBEphemeralStateStore("existing-metadata", table=table, clock=lambda: 100.0)
        operation = IngestionOperationRecord(
            operation_id="ing_active", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="STARTING", created_at=100.0, updated_at=100.0, expires_at=200.0,
        )
        store.put_ingestion_operation(operation)
        self.assertEqual(store.get_ingestion_operation_for_document_set(operation.document_set_key).operation_id, operation.operation_id)
        store.update_ingestion_operation(
            replace(operation, status="INDEXED", provider_status="COMPLETE", updated_at=100.0)
        )
        self.assertIsNone(store.get_ingestion_operation_for_document_set(operation.document_set_key))
        self.assertFalse(any(name == "scan" for name, _kwargs in table.calls))


if __name__ == "__main__":
    unittest.main()
