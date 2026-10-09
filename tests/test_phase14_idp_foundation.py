from __future__ import annotations

import sys
import unittest
import json
from threading import Barrier, Thread
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    DocumentForIDP,
    DocumentType,
    IDPCheckpoint,
    IDPConfig,
    IDPContractError,
    IDPDeliveryAmbiguous,
    IDPExtractionRun,
    IDPFieldResult,
    IDPJob,
    IDPJobStatus,
    FieldPresence,
    FieldOrigin,
    FieldAcceptance,
    IDP_SCHEMA_VERSION,
    IDPSchemaRegistry,
    IDPSourceArnError,
    IDPWorker,
    IDPPaidCallGate,
    Boto3DynamoIDPRepository,
    IDPConcurrencyError,
    InMemoryIDPRepository,
    create_verified_clean_job,
    enqueue_verified_clean_job,
    idempotency_key,
    matter_partition_key,
    new_id,
    redeliver_ambiguous_job,
    run_sort_key,
)
from legaldesk.idp.persistence import (
    _idempotency_item,
    _job_item,
    _locator_item,
    _serialize_transaction_item,
    job_locator_partition_key,
    job_locator_sort_key,
    job_sort_key,
)


def document(**overrides: object) -> DocumentForIDP:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "matter_id": "matter-a",
        "document_id": "doc-a",
        "media_type": "application/pdf",
        "file_size_bytes": 1024,
        "malware_scan_clean": True,
        "page_count": 2,
        "content_sha256": "a" * 64,
    }
    values.update(overrides)
    return DocumentForIDP(**values)


class Queue:
    def __init__(self, error: Exception | None = None) -> None:
        self.ids: list[str] = []
        self.error = error

    def publish(self, *, job_id: str) -> None:
        if self.error:
            raise self.error
        self.ids.append(job_id)


class BotoExpressionTable:
    def __init__(self, items: dict[tuple[str, str], dict[str, object]] | None = None, client: object | None = None) -> None:
        self.items = items or {}
        self.calls: list[dict[str, object]] = []
        self.meta = type("Meta", (), {"client": client})()

    def get_item(self, *, Key, **kwargs):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs):
        self.calls.append({"put_item": kwargs})

    def update_item(self, **kwargs):
        self.calls.append({"update_item": kwargs})


class TestIDPFoundation(unittest.TestCase):
    def test_registry_is_versioned_and_unknown_is_not_forced_into_contract(self) -> None:
        registry = IDPSchemaRegistry()
        self.assertEqual(registry.get(DocumentType.CONTRACT).version, IDP_SCHEMA_VERSION)
        self.assertIn("operative_ruling", registry.get(DocumentType.JUDGMENT).fields)
        self.assertEqual(set(registry.get(DocumentType.UNKNOWN).fields), {"document_type", "general_document_evidence"})
        self.assertNotIn("effective_date", registry.get(DocumentType.UNKNOWN).fields)

    def test_field_contract_distinguishes_presence_from_null(self) -> None:
        with self.assertRaises(IDPContractError):
            IDPFieldResult(field="effective_date", value=None, presence="PRESENT")  # type: ignore[arg-type]
        absent = IDPFieldResult(field="effective_date", presence=FieldPresence.ABSENT)
        self.assertIsNone(absent.value)

    def test_models_validate_hash_timezone_and_immutable_run_fields(self) -> None:
        with self.assertRaises(IDPContractError):
            document(content_sha256="not-a-sha")
        with self.assertRaises(IDPContractError):
            IDPJob(job_id="job", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="idem", created_at=datetime.now())
        field = IDPFieldResult(field="court", value="Court", presence=FieldPresence.PRESENT, origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.AUTO_ACCEPTED)
        run = IDPExtractionRun(run_id="run", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, document_type=DocumentType.DEMAND, schema_version=IDP_SCHEMA_VERSION, model_id="model", prompt_version="prompt", status=IDPJobStatus.COMPLETED, fields={"court": field})
        with self.assertRaises(TypeError):
            run.fields["court"] = field  # type: ignore[index]

    def test_idempotency_is_scoped_and_duplicate_does_not_create_second_job(self) -> None:
        repository = InMemoryIDPRepository()
        first = create_verified_clean_job(repository=repository, document=document())
        retry = create_verified_clean_job(repository=repository, document=document())
        self.assertEqual(first.job_id, retry.job_id)
        with self.assertRaises(IDPContractError):
            repository.create_job(IDPJob(
                job_id=new_id(), tenant_id=first.tenant_id, matter_id=first.matter_id,
                document_id=first.document_id, document_sha256="b" * 64,
                idempotency_key=first.idempotency_key,
            ))
        self.assertEqual(idempotency_key(document=document()), first.idempotency_key)

    def test_same_idempotency_text_is_independent_across_tenants(self) -> None:
        repository = InMemoryIDPRepository()
        first = IDPJob(job_id="job-a", tenant_id="tenant-a", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="same")
        second = IDPJob(job_id="job-b", tenant_id="tenant-b", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="same")
        self.assertEqual(repository.create_job(first).job_id, "job-a")
        self.assertEqual(repository.create_job(second).job_id, "job-b")

    def test_config_rejects_float_coercion(self) -> None:
        with self.assertRaises(IDPContractError):
            IDPConfig.from_mapping({"max_bytes": "20.5"})

    def test_verified_clean_producer_stops_on_ambiguous_publish(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        with self.assertRaises(IDPDeliveryAmbiguous):
            enqueue_verified_clean_job(repository=repository, queue=Queue(RuntimeError("unknown")), job=job)
        self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.DELIVERY_AMBIGUOUS)  # type: ignore[union-attr]

        clean = create_verified_clean_job(repository=repository, document=document(document_id="doc-c"))
        queue = Queue()
        delivered = enqueue_verified_clean_job(repository=repository, queue=queue, job=clean)
        self.assertEqual(delivered.status, IDPJobStatus.QUEUED)
        self.assertEqual(queue.ids, [clean.job_id])

    def test_publish_finalize_race_is_explicitly_redeliverable(self) -> None:
        class FinalizeRaceRepository(InMemoryIDPRepository):
            fail_once = True

            def mark_delivery(self, *, job_id, status):
                if status is IDPJobStatus.QUEUED and self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("conditional finalization race")
                return super().mark_delivery(job_id=job_id, status=status)

        repository = FinalizeRaceRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        with self.assertRaises(IDPDeliveryAmbiguous):
            enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.DELIVERY_AMBIGUOUS)  # type: ignore[union-attr]
        delivered = redeliver_ambiguous_job(repository=repository, queue=Queue(), job_id=job.job_id)
        self.assertEqual(delivered.status, IDPJobStatus.QUEUED)

    def test_worker_uses_persisted_scope_and_returns_partial_failures(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        looked_up: list[tuple[str, str, str]] = []

        def lookup(*, tenant_id: str, matter_id: str, document_id: str) -> DocumentForIDP:
            looked_up.append((tenant_id, matter_id, document_id))
            return document()

        worker = IDPWorker(repository=repository, document_lookup=lookup, config=IDPConfig(), expected_source_arn="arn:aws:sqs:eu-west-1:123:legaldesk-idp", worker_id="worker-a")
        result = worker.handle_sqs_event({"Records": [
            {"messageId": "m-1", "eventSourceARN": "arn:aws:sqs:eu-west-1:123:legaldesk-idp", "body": '{"jobId": "' + job.job_id + '"}'},
            {"messageId": "m-2", "eventSourceARN": "arn:aws:sqs:eu-west-1:123:legaldesk-idp", "body": "not-json"},
        ]})
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m-2"}]})
        self.assertEqual(looked_up, [("tenant-a", "matter-a", "doc-a")])
        self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.PROCESSING)  # type: ignore[union-attr]

    def test_wrong_source_or_queue_scope_is_rejected(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        worker = IDPWorker(repository=repository, document_lookup=lambda **kwargs: document(), config=IDPConfig(), expected_source_arn="arn:expected", worker_id="worker-a")
        with self.assertRaises(IDPSourceArnError):
            worker.handle_sqs_event({"Records": [{"messageId": "m", "eventSourceARN": "arn:wrong", "body": "{}"}]})
        with self.assertRaises(IDPContractError):
            worker.process_message({"jobId": job.job_id, "tenantId": "attacker"})

    def test_size_and_type_skip_without_changing_rag_document(self) -> None:
        repository = InMemoryIDPRepository()
        text_job = create_verified_clean_job(repository=repository, document=document(document_id="doc-text", media_type="text/plain"))
        oversized_job = create_verified_clean_job(repository=repository, document=document(document_id="doc-large", file_size_bytes=IDPConfig().max_bytes + 1))
        queue = Queue()
        enqueue_verified_clean_job(repository=repository, queue=queue, job=text_job)
        enqueue_verified_clean_job(repository=repository, queue=queue, job=oversized_job)
        lookup = lambda *, tenant_id, matter_id, document_id: document(document_id=document_id, media_type="text/plain" if document_id == "doc-text" else "application/pdf", file_size_bytes=IDPConfig().max_bytes + 1 if document_id == "doc-large" else 2)
        worker = IDPWorker(repository=repository, document_lookup=lookup, config=IDPConfig(), expected_source_arn="arn:expected", worker_id="worker-a")
        result = worker.handle_sqs_event({"Records": [{"messageId": job_id, "eventSourceARN": "arn:expected", "body": '{"jobId": "' + job_id + '"}'} for job_id in queue.ids]})
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(repository.get_job(text_job.job_id).status, IDPJobStatus.SKIPPED)  # type: ignore[union-attr]
        self.assertEqual(repository.get_job(oversized_job.job_id).status, IDPJobStatus.SKIPPED)  # type: ignore[union-attr]

    def test_paid_call_checkpoint_is_not_automatically_reentered(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        claim = repository.claim_job(job_id=job.job_id, worker_id="worker-a", lease_seconds=30, now=datetime.now(timezone.utc))
        gate = IDPPaidCallGate(repository, claim)
        gate.ready()
        gate.begin()
        with self.assertRaises(Exception):
            gate.begin()
        gate.ambiguous()
        with self.assertRaises(Exception):
            gate.committed()

    def test_concurrent_paid_call_begin_has_one_atomic_winner(self) -> None:
        class CoordinatedRepository(InMemoryIDPRepository):
            barrier = Barrier(2)

            def get_job(self, job_id):
                result = super().get_job(job_id)
                if result is not None and result.checkpoint is IDPCheckpoint.PAID_CALL_READY:
                    self.barrier.wait(timeout=5)
                return result

        repository = CoordinatedRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        claim = repository.claim_job(job_id=job.job_id, worker_id="worker", lease_seconds=30, now=datetime.now(timezone.utc))
        repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.PAID_CALL_READY)
        outcomes: list[Exception | None] = []

        def begin() -> None:
            try:
                IDPPaidCallGate(repository, claim).begin()
                outcomes.append(None)
            except Exception as exc:  # noqa: BLE001 - assertion captures the loser
                outcomes.append(exc)

        threads = [Thread(target=begin) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(outcome is None for outcome in outcomes), 1)
        self.assertEqual(sum(isinstance(outcome, IDPConcurrencyError) for outcome in outcomes), 1)

    def test_boto_claim_expression_omits_null_claim_and_names_claim_attribute(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)
        items = {
            (job_locator_partition_key(job.job_id), job_locator_sort_key()): _locator_item(job),
            (matter_partition_key(job.tenant_id, job.matter_id), job_sort_key(job.job_id)): _job_item(replace(job, status=IDPJobStatus.QUEUED)),
        }
        table = BotoExpressionTable(items)
        boto = Boto3DynamoIDPRepository("table", table=table, allow_non_atomic_test_adapter=True)
        boto.claim_job(job_id=job.job_id, worker_id="worker", lease_seconds=30, now=datetime.now(timezone.utc))
        update = table.calls[-1]["update_item"]
        self.assertIn("#claim", update["ExpressionAttributeNames"])
        self.assertNotIn(":noClaim", update["ExpressionAttributeValues"])
        self.assertIn("attribute_not_exists(#claim)", update["ConditionExpression"])

    def test_boto_transaction_uses_sdk_attribute_value_serialization(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = []

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        table = BotoExpressionTable(client=client)
        boto = Boto3DynamoIDPRepository("table", table=table, transaction_client=client)
        job = IDPJob(job_id="job", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="idem")
        self.assertEqual(boto.create_job(job), job)
        transaction = client.calls[0]["TransactItems"]
        self.assertEqual(len(transaction), 3)
        self.assertTrue(all(isinstance(next(iter(item["Put"]["Item"].values())), str) for item in transaction))
        self.assertNotIn("claimToken", transaction[0]["Put"]["Item"])

    def test_real_boto_resource_hook_sees_single_serialization_without_network(self) -> None:
        import boto3

        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        captured: dict[str, object] = {}

        class AbortBeforeNetwork(Exception):
            pass

        def before_call(model, params, **kwargs):
            captured.update(params)
            raise AbortBeforeNetwork()

        table.meta.client.meta.events.register("before-call.dynamodb.TransactWriteItems", before_call)
        item = {"pk": "TENANT#t#MATTER#m", "sk": "IDP#JOB#j"}
        with self.assertRaises(AbortBeforeNetwork):
            table.meta.client.transact_write_items(TransactItems=[{"Put": {"TableName": "table", "Item": _serialize_transaction_item(item)}}])
        body = json.loads(captured["body"])
        self.assertEqual(body["TransactItems"][0]["Put"]["Item"]["pk"], {"S": "TENANT#t#MATTER#m"})

    def test_repository_defaults_to_resource_client_and_serializes_create_job_once(self) -> None:
        import boto3

        job = IDPJob(job_id="sdk-job", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="idem")
        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        table.get_item = Mock(return_value={})
        table.query = Mock(return_value={"Items": []})
        captured: dict[str, object] = {}

        class AbortBeforeNetwork(Exception):
            pass

        def before_call(model, params, **kwargs):
            captured.update(params)
            raise AbortBeforeNetwork()

        table.meta.client.meta.events.register("before-call.dynamodb.TransactWriteItems", before_call)
        boto = Boto3DynamoIDPRepository("table", table=table)
        # The adapter treats the deliberate before-call abort as a failed
        # transaction, but the captured request proves no network was used.
        with self.assertRaises(IDPConcurrencyError):
            boto.create_job(job)
        body = json.loads(captured["body"])
        self.assertEqual(body["TransactItems"][0]["Put"]["Item"]["pk"]["S"], "TENANT#tenant#MATTER#matter")

    def test_boto_transaction_duplicate_rereads_winner(self) -> None:
        job = IDPJob(job_id="winner", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="idem")
        table = BotoExpressionTable()

        class RacingClient:
            def transact_write_items(self, **kwargs):
                table.items[(job_locator_partition_key(job.job_id), job_locator_sort_key())] = _locator_item(job)
                table.items[(matter_partition_key(job.tenant_id, job.matter_id), job_sort_key(job.job_id))] = _job_item(job)
                table.items[(matter_partition_key(job.tenant_id, job.matter_id), "IDP#IDEMPOTENCY#idem")] = _idempotency_item(job)
                raise RuntimeError("transaction canceled by concurrent winner")

        table.meta.client = RacingClient()
        boto = Boto3DynamoIDPRepository("table", table=table, transaction_client=table.meta.client)
        self.assertEqual(boto.create_job(replace(job, job_id="loser")), job)

    def test_expired_lease_and_backward_checkpoint_are_fenced(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        now = datetime.now(timezone.utc)
        claim = repository.claim_job(job_id=job.job_id, worker_id="worker", lease_seconds=1, now=now)
        repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.PAID_CALL_READY, now=now)
        with self.assertRaises(IDPConcurrencyError):
            repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.CREATED, now=now)
        with self.assertRaises(IDPConcurrencyError):
            repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.PAID_CALL_IN_FLIGHT, now=claim.claimed_until)

    def test_expired_prepaid_claim_can_reclaim_but_inflight_paid_call_cannot(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        now = datetime.now(timezone.utc)
        first = repository.claim_job(job_id=job.job_id, worker_id="worker-a", lease_seconds=1, now=now)
        second = repository.claim_job(job_id=job.job_id, worker_id="worker-b", lease_seconds=1, now=first.claimed_until)
        self.assertNotEqual(first.claim_token, second.claim_token)
        repository.checkpoint_job(claim=second, checkpoint=IDPCheckpoint.PAID_CALL_READY, now=now)
        repository.checkpoint_job(claim=second, checkpoint=IDPCheckpoint.PAID_CALL_IN_FLIGHT, now=now)
        with self.assertRaises(IDPConcurrencyError):
            repository.claim_job(job_id=job.job_id, worker_id="worker-c", lease_seconds=1, now=second.claimed_until)

    def test_boto_checkpoint_condition_names_claim(self) -> None:
        job = IDPJob(job_id="job", tenant_id="tenant", matter_id="matter", document_id="doc", document_sha256="a" * 64, idempotency_key="idem", status=IDPJobStatus.CLAIMED, checkpoint=IDPCheckpoint.CREATED, claim_token="claim", claimed_until=datetime.now(timezone.utc).replace(microsecond=0).replace(year=2099))
        table = BotoExpressionTable({
            (job_locator_partition_key(job.job_id), job_locator_sort_key()): _locator_item(job),
            (matter_partition_key(job.tenant_id, job.matter_id), job_sort_key(job.job_id)): _job_item(job),
        })
        boto = Boto3DynamoIDPRepository("table", table=table, allow_non_atomic_test_adapter=True)
        boto.checkpoint_job(claim=type("Claim", (), {"job_id": job.job_id, "claim_token": "claim"})(), checkpoint=IDPCheckpoint.PAID_CALL_READY)  # type: ignore[arg-type]
        update = table.calls[-1]["update_item"]
        self.assertEqual(update["ExpressionAttributeNames"]["#claim"], "claimToken")

    def test_locator_requires_persisted_content_hash(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document())
        from legaldesk.idp.persistence import AuthoritativeJobLocator
        locator = AuthoritativeJobLocator(repository, lambda **kwargs: document(content_sha256="b" * 64))
        with self.assertRaises(IDPConcurrencyError):
            locator.locate({"jobId": job.job_id})

    def test_bounded_delivery_cursor_does_not_hide_later_pending_jobs(self) -> None:
        repository = InMemoryIDPRepository()
        jobs = []
        for index in range(3):
            item = create_verified_clean_job(repository=repository, document=document(document_id=f"doc-{index}"))
            jobs.append(item)
            repository.mark_delivery(job_id=item.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
            if index < 2:
                repository.mark_delivery(job_id=item.job_id, status=IDPJobStatus.QUEUED)
        page = repository.list_delivery_candidates(tenant_id="tenant-a", matter_id="matter-a", limit=1)
        self.assertEqual(tuple(job.job_id for job in page.jobs), (jobs[2].job_id,))
        self.assertIsNone(page.next_cursor)

    def test_option_b_keys_are_bounded_and_no_scan_shape_is_needed(self) -> None:
        self.assertEqual(matter_partition_key("tenant-a", "matter-a"), "TENANT#tenant-a#MATTER#matter-a")
        self.assertEqual(run_sort_key("doc-a", "run-a"), "IDP#RUN#doc-a#run-a")


if __name__ == "__main__":
    unittest.main()
