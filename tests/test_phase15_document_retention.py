"""Ghost documents (T9 step 12, 2026-11-03): objects expired by S3 while the record and the Knowledge Base stayed behind.

* No lifecycle rule may expire canonical originals unless the repository also holds the code that keeps record and KB consistent.
* The reconciliation Lambda finds INDEXED documents without an object, one closed line each with a probable cause, and alarms. It repairs nothing.
* The change sets that deploy this are pinned by profile.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT))

from legaldesk import reconciliation as rec  # noqa: E402
from legaldesk import reconciliation_lambda as rl  # noqa: E402
from legaldesk.documents import Boto3S3ObjectStorage, InMemoryDocumentMetadataRepository, InMemoryObjectStorage  # noqa: E402
from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus  # noqa: E402
from scripts import phase15_changeset_preview as preview  # noqa: E402

TEMPLATES = ROOT / "infra" / "cloudformation"
FIXTURES = ROOT / "tests" / "fixtures" / "phase15_live"
GOVERNANCE_RUNBOOK = ROOT / "docs" / "phase-14-data-governance-runbook.md"
TENANT, MATTER = "tnt_phase14_20260928", "mat_phase14_a_20260928"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node, deep=True)))


def template(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def s3_rules(cloudformation_rules: list[dict]) -> list[dict]:
    """The CloudFormation lifecycle rules in the shape ``GetBucketLifecycleConfiguration`` returns them."""

    rules = []
    for rule in cloudformation_rules:
        item = {"ID": rule["Id"], "Status": rule["Status"], "Filter": {"Prefix": rule.get("Prefix", "")}}
        if "ExpirationInDays" in rule:
            item["Expiration"] = {"Days": rule["ExpirationInDays"]}
        if "ExpirationDate" in rule:
            item["Expiration"] = {"Date": rule["ExpirationDate"]}
        rules.append(item)
    return rules


def lifecycle_of(path: Path) -> list[dict]:
    return template(path)["Resources"]["DocumentBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]


def retention_violations(path: Path) -> list[str]:
    """What the repository guard checks: an expiry on canonical originals needs the consistency code, or the template is refused."""

    expiry = rec.canonical_expiry(s3_rules(lifecycle_of(path)))
    if expiry.configured and not rec.CANONICAL_EXPIRY_KEEPS_RECORDS_CONSISTENT:
        return [f"canonical originals expire after {expiry.window_days} days but nothing keeps the records and the Knowledge Base consistent"]
    return []


class NoExpiryForCanonicalOriginalsTests(unittest.TestCase):
    def test_the_template_has_only_the_quarantine_expiry(self) -> None:
        rules = lifecycle_of(TEMPLATES / "phase-02-document-pipeline.yaml")
        self.assertEqual([(r["Id"], r["Prefix"], r["ExpirationInDays"]) for r in rules], [("ExpireQuarantineUploads", "quarantine/", 1)])
        self.assertEqual(retention_violations(TEMPLATES / "phase-02-document-pipeline.yaml"), [])

    def test_the_guard_refuses_the_rule_that_caused_the_ghosts(self) -> None:
        before = FIXTURES / "phase-02-document-pipeline.before-no-expiry.yaml"  # the template as it was deployed on 2026-11-03
        self.assertEqual([r["Id"] for r in lifecycle_of(before)], ["ExpireQuarantineUploads", "ExpireFictionalOriginals"])
        violations = retention_violations(before)
        self.assertEqual(len(violations), 1)
        self.assertIn("30 days", violations[0])

    def test_the_guard_sees_every_way_to_expire_tenants_objects(self) -> None:
        for prefix in ("", "tenants/", "tenants/tnt_phase14_20260928/", "ten"):
            with self.subTest(prefix=prefix):
                self.assertTrue(rec.canonical_expiry([{"Status": "Enabled", "Filter": {"Prefix": prefix}, "Expiration": {"Days": 60}}]).configured)
        self.assertTrue(rec.canonical_expiry([{"Status": "Enabled", "Prefix": "", "Expiration": {"Days": 5}}]).configured)  # legacy shape
        self.assertTrue(rec.canonical_expiry([{"Status": "Enabled", "Filter": {"And": {"Prefix": "tenants/", "Tags": [{"Key": "k", "Value": "v"}]}}, "Expiration": {"Days": 9}}]).configured)
        self.assertTrue(rec.canonical_expiry([{"Status": "Enabled", "Filter": {"Tag": {"Key": "k", "Value": "v"}}, "Expiration": {"Days": 9}}]).configured)  # no prefix: assume it applies
        date_rule = rec.canonical_expiry([{"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"Date": "2027-01-01T00:00:00Z"}}])
        self.assertEqual((date_rule.configured, date_rule.window_days), (True, None))
        self.assertEqual(rec.canonical_expiry([{"Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 30}},
                                               {"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"Days": 7}}]).window_days, 7)

    def test_rules_that_cannot_remove_a_current_canonical_object_do_not_count(self) -> None:
        for rule in (
            {"Status": "Enabled", "Filter": {"Prefix": "quarantine/"}, "Expiration": {"Days": 1}},
            {"Status": "Disabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"Days": 30}},
            {"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"ExpiredObjectDeleteMarker": True}},
            {"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
            {"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]},
        ):
            with self.subTest(rule=rule):
                self.assertEqual(rec.canonical_expiry([rule]).state, "none")

    def test_the_contract_flag_is_false_today_so_reintroducing_an_expiry_needs_new_code(self) -> None:
        self.assertIs(rec.CANONICAL_EXPIRY_KEEPS_RECORDS_CONSISTENT, False)
        with mock.patch.object(rec, "CANONICAL_EXPIRY_KEEPS_RECORDS_CONSISTENT", True):
            self.assertEqual(retention_violations(FIXTURES / "phase-02-document-pipeline.before-no-expiry.yaml"), [])

    def test_the_phase02_change_set_is_exactly_the_profile(self) -> None:
        before = preview.load_template((FIXTURES / "phase-02-document-pipeline.before-no-expiry.yaml").read_text(encoding="utf-8"))
        after = preview.load_template((TEMPLATES / "phase-02-document-pipeline.yaml").read_text(encoding="utf-8"))
        result = preview.compare(dict(before), dict(after), {}, {})
        self.assertEqual({item["id"]: item["properties"] for item in result["modified"]}, {"DocumentBucket": ["LifecycleConfiguration"]})
        self.assertEqual((result["added"], result["removed"], result["iam"]), ([], [], []))
        # The registry schema of AWS::S3::Bucket is not recorded (and an offline tool must not invent it): the preview says UNKNOWN,
        # the live change set decides. A Replacement of False passes the gate; anything else stops it.
        self.assertEqual([item["replacement"] for item in result["modified"]], ["UNKNOWN"])
        profile = preview.load_profile("phase02-no-canonical-expiry")
        live = {"Status": "CREATE_COMPLETE", "Changes": [{"ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "DocumentBucket", "ResourceType": "AWS::S3::Bucket", "Replacement": "False",
            "Details": [{"Target": {"Name": "LifecycleConfiguration", "Attribute": "Properties", "RequiresRecreation": "Never"}, "Evaluation": "Static", "ChangeSource": "DirectModification"}]}}]}
        self.assertEqual(preview.analyze_change_set(live, profile)["violations"], [])
        replaced = copy.deepcopy(live)
        replaced["Changes"][0]["ResourceChange"]["Replacement"] = "True"
        self.assertTrue(preview.analyze_change_set(replaced, profile)["violations"])


class FakeS3Client:
    def __init__(self, keys=(), *, rules=None, rules_error=None, list_error=None):
        self.keys, self.rules, self.rules_error, self.list_error, self.calls = sorted(keys), rules, rules_error, list_error, []

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        if self.list_error:
            raise self.list_error
        matching = [k for k in self.keys if k.startswith(kwargs["Prefix"])][: kwargs["MaxKeys"]]
        return {"Contents": [{"Key": k} for k in matching]} if matching else {"KeyCount": 0}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.calls.append(("get_bucket_lifecycle_configuration", kwargs))
        if self.rules_error:
            raise self.rules_error
        return {"Rules": self.rules}


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class TheS3AdapterAnswersExistenceWithoutReadingAnythingTests(unittest.TestCase):
    KEY = f"tenants/{TENANT}/matters/{MATTER}/documents/aaa/original.txt"

    def test_existence_is_the_exact_key_not_a_longer_one_that_starts_with_it(self) -> None:
        sidecar_only = FakeS3Client([self.KEY + ".metadata.json"])
        self.assertFalse(Boto3S3ObjectStorage("bucket", client=sidecar_only).object_exists(key=self.KEY))
        self.assertTrue(Boto3S3ObjectStorage("bucket", client=FakeS3Client([self.KEY, self.KEY + ".metadata.json"])).object_exists(key=self.KEY))
        name, kwargs = sidecar_only.calls[0]
        self.assertEqual((name, kwargs), ("list_objects_v2", {"Bucket": "bucket", "Prefix": self.KEY, "MaxKeys": 1}))  # the call the IAM prefix condition authorises

    def test_an_error_is_an_error_not_a_missing_object(self) -> None:
        with self.assertRaises(ClientError):
            Boto3S3ObjectStorage("bucket", client=FakeS3Client(list_error=ClientError("AccessDenied"))).object_exists(key=self.KEY)

    def test_no_lifecycle_configuration_is_an_empty_list_and_other_errors_propagate(self) -> None:
        self.assertEqual(Boto3S3ObjectStorage("b", client=FakeS3Client(rules_error=ClientError("NoSuchLifecycleConfiguration"))).lifecycle_rules(), ())
        with self.assertRaises(ClientError):
            Boto3S3ObjectStorage("b", client=FakeS3Client(rules_error=ClientError("AccessDenied"))).lifecycle_rules()
        self.assertEqual(len(Boto3S3ObjectStorage("b", client=FakeS3Client(rules=[{"ID": "x"}, "junk"])).lifecycle_rules()), 1)


def document(index: int, *, status=DocumentStatus.INDEXED, scan=MalwareScanStatus.CLEAN, age_days: int = 36, key: str | None = None, name="Contrato.pdf") -> Document:
    document_id = f"6f1d0c1e-0000-4000-8000-{index:012d}"
    return Document(
        document_id=document_id, matter_id=MATTER, tenant_id=TENANT, name=name,
        s3_key=key or f"tenants/{TENANT}/matters/{MATTER}/documents/{document_id}/original.txt", media_type="text/plain",
        jurisdiction="ES", document_date="2026-09-28", confidentiality="internal", status=status, malware_scan_status=scan,
        uploaded_at=NOW - timedelta(days=age_days),
    )


class World:
    def __init__(self, documents, present, rules=(), *, storage_fail=False):
        self.repository = InMemoryDocumentMetadataRepository()
        for item in documents:
            self.repository.save(item)
        self.storage = InMemoryObjectStorage(fail=storage_fail)
        self.storage.lifecycle = tuple(rules)
        for key in present:
            self.storage.objects[key] = b"x"
        self.service = rec.ReconciliationService(
            metadata_repository=self.repository, object_storage=self.storage, state_store=mock.Mock(), clock=lambda: NOW.timestamp(),
        )

    def run(self, **kwargs):
        return self.service.reconcile_missing_objects(scopes=[rec.ReconciliationScope(TENANT, MATTER)], **kwargs)


THIRTY_DAY_RULE = {"ID": "ExpireFictionalOriginals", "Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"Days": 30}}


class MissingObjectsAreFoundAndExplainedTests(unittest.TestCase):
    def test_a_missing_object_older_than_the_lifecycle_window_is_a_probable_lifecycle_expiry(self) -> None:
        ghost, healthy = document(1, age_days=36), document(2)
        report = World([ghost, healthy], [healthy.s3_key], [THIRTY_DAY_RULE]).run()
        self.assertEqual([(f.document_id, f.status, f.age_days, f.probable_cause) for f in report.findings],
                         [(ghost.document_id, "INDEXED", 36, "s3_lifecycle_expiration")])
        self.assertEqual((report.examined, report.skipped, report.failed, report.changed), (2, 1, 0, 0))

    def test_every_other_case_says_the_cause_is_unknown(self) -> None:
        cases = {
            "unknown_younger_than_lifecycle_window": ([THIRTY_DAY_RULE], 10),
            "unknown_no_lifecycle_expiry": ([], 90),
            "unknown_lifecycle_date_rule": ([{"Status": "Enabled", "Filter": {"Prefix": "tenants/"}, "Expiration": {"Date": "2026-10-01T00:00:00Z"}}], 90),
        }
        for label, (rules, age) in cases.items():
            with self.subTest(label):
                report = World([document(1, age_days=age)], [], rules).run()
                self.assertEqual([f.probable_cause for f in report.findings], [label])
        self.assertTrue(all(label == "s3_lifecycle_expiration" or label.startswith("unknown_") for label in rec.PROBABLE_CAUSES))

    def test_an_unreadable_lifecycle_never_hides_the_missing_object(self) -> None:
        world = World([document(1)], [])

        class Unreadable:
            def __init__(self, inner):
                self.inner = inner

            def object_exists(self, **kwargs):
                return self.inner.object_exists(**kwargs)

            def lifecycle_rules(self):
                raise ClientError("AccessDenied")

        world.service.object_storage = Unreadable(world.storage)
        report = world.run()
        self.assertEqual([f.probable_cause for f in report.findings], ["unknown_lifecycle_unreadable"])
        self.assertEqual(report.canonical_expiry.state, "unreadable")

    def test_only_indexed_clean_documents_are_checked_and_nothing_is_changed(self) -> None:
        records = [document(1, status=DocumentStatus.FAILED), document(2, scan=MalwareScanStatus.PENDING), document(3, status=DocumentStatus.PENDING_INGESTION), document(4)]
        world = World(records, [])
        report = world.run()
        self.assertEqual([f.document_id for f in report.findings], [records[3].document_id])
        self.assertEqual(report.examined, 1)
        self.assertEqual({d.status for d in world.repository.documents.values()} & {DocumentStatus.FAILED}, {DocumentStatus.FAILED})  # only the one that was already FAILED
        self.assertEqual(len(world.repository.documents), 4)  # report only: no tombstone, no delete
        self.assertEqual(world.storage.objects, {})

    def test_a_key_outside_the_canonical_prefix_is_ambiguous_not_missing(self) -> None:
        report = World([document(1, key="tenants/other/matters/x/documents/y/original.txt")], []).run()
        self.assertEqual((report.findings, report.ambiguous, report.examined), ([], 1, 0))

    def test_a_failing_check_is_failed_not_missing(self) -> None:
        report = World([document(1)], [], storage_fail=True).run()
        self.assertEqual((report.findings, report.failed), ([], 1))

    def test_it_is_bounded_and_keyed(self) -> None:
        many = [document(i) for i in range(1, 8)]
        world = World(many, [])
        calls = []
        real = world.repository.list_for_scope

        class Counting:
            def list_for_scope(self, **kwargs):
                calls.append(kwargs)
                return real(**kwargs)

        world.service.metadata_repository = Counting()
        report = world.run(limit_per_scope=3)
        self.assertEqual([c["limit"] for c in calls], [3])  # one keyed partition query per scope, bounded
        self.assertLessEqual(len(report.findings), 3)
        with self.assertRaises(ValueError):
            world.run(limit_per_scope=1000)

    def test_the_budget_guard_stops_the_pass_without_error(self) -> None:
        report = World([document(1), document(2)], []).run(has_budget=lambda: False)
        self.assertEqual((report.findings, report.examined), ([], 0))

    def test_a_finding_carries_no_key_tenant_or_content(self) -> None:
        report = World([document(1, name="SECRET-CONTRACT-NAME.pdf")], []).run()
        text = json.dumps([vars(f) if hasattr(f, "__dict__") else {k: getattr(f, k) for k in f.__slots__} for f in report.findings])
        for forbidden in (TENANT, MATTER, "tenants/", "SECRET-CONTRACT-NAME", ".txt"):
            self.assertNotIn(forbidden, text)


class TheLambdaLogsOneClosedLinePerGhostAndAlarmsOnTheSummaryTests(unittest.TestCase):
    def emit(self, report: rec.ReconciliationReport) -> tuple[dict, list[dict]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summary = rl._emit_object_findings(report)
        return summary, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_the_per_document_line_is_diagnosable_on_its_own(self) -> None:
        report = World([document(1, age_days=36)], [], [THIRTY_DAY_RULE]).run()
        summary, lines = self.emit(report)
        ghost = next(line for line in lines if line["event"] == "document_object_missing")
        self.assertEqual(ghost, {"event": "document_object_missing", "code": "object_missing", "documentId": document(1).document_id,
                                 "status": "INDEXED", "ageDays": 36, "probableCause": "s3_lifecycle_expiration"})
        self.assertEqual(summary, {"examined": 1, "missing": 1, "failed": 0, "skipped": 0})
        text = json.dumps(lines)
        for forbidden in (TENANT, MATTER, "tenants/", "Contrato", "original"):
            self.assertNotIn(forbidden, text)

    def test_the_summary_counts_findings_and_adds_one_while_an_expiry_exists(self) -> None:
        _, lines = self.emit(World([document(1)], [], [THIRTY_DAY_RULE]).run())
        check = next(line for line in lines if line["event"] == "document_objects_check")
        self.assertEqual((check["missing"], check["findings"], check["canonicalExpiry"], check["lifecycleWindowDays"]), (1, 2, "configured", 30))
        # a healthy world with an expiry rule still alarms: that configuration is what creates ghosts
        healthy = document(1)
        _, lines = self.emit(World([healthy], [healthy.s3_key], [THIRTY_DAY_RULE]).run())
        check = next(line for line in lines if line["event"] == "document_objects_check")
        self.assertEqual((check["missing"], check["findings"]), (0, 1))

    def test_a_healthy_world_without_expiry_is_silent(self) -> None:
        healthy = document(1)
        summary, lines = self.emit(World([healthy], [healthy.s3_key], []).run())
        self.assertEqual((lines, summary["missing"]), ([], 0))

    def test_a_failing_check_is_reported_without_a_finding(self) -> None:
        _, lines = self.emit(World([document(1)], [], storage_fail=True).run())
        check = next(line for line in lines if line["event"] == "document_objects_check")
        self.assertEqual((check["failed"], check["findings"]), (1, 0))

    def test_the_handler_wires_the_check_and_survives_its_failure(self) -> None:
        source = (ROOT / "backend" / "src" / "legaldesk" / "reconciliation_lambda.py").read_text(encoding="utf-8")
        self.assertIn("reconcile_missing_objects(", source)
        self.assertIn('"objects": _emit_object_findings(objects)', source)


class TheStackIsPinnedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (TEMPLATES / "phase-14-reconciliation.yaml").read_text(encoding="utf-8")
        self.template = template(TEMPLATES / "phase-14-reconciliation.yaml")

    def statements(self) -> dict[str, dict]:
        policy = self.template["Resources"]["ReconciliationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        return {s["Sid"]: s for s in policy if isinstance(s, dict) and "Sid" in s}

    def test_the_two_new_permissions_are_exact_and_read_only(self) -> None:
        statements = self.statements()
        listing = statements["CheckBetaCanonicalObjectsExistByListingTheExactKey"]
        self.assertEqual((listing["Action"], listing["Resource"]), ("s3:ListBucket", "SourceBucketArn"))
        self.assertEqual(listing["Condition"], {"StringLike": {"s3:prefix": [
            "tenants/${BetaTenantId}/matters/*/documents/*/original.pdf", "tenants/${BetaTenantId}/matters/*/documents/*/original.txt"]}})
        lifecycle = statements["ReadBucketLifecycleRulesToExplainMissingObjects"]
        self.assertEqual((lifecycle["Action"], lifecycle["Resource"], "Condition" in lifecycle), ("s3:GetLifecycleConfiguration", "SourceBucketArn", False))
        # no new write and no object read: s3:GetObject stays confined to the opt-in IDP statement
        every_action = [a for s in statements.values() for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertEqual(sorted(a for a in every_action if a.startswith("s3:")), ["s3:DeleteObject", "s3:GetLifecycleConfiguration", "s3:ListBucket"])

    def test_the_alarm_says_what_it_means_and_points_to_a_runbook_section_that_exists(self) -> None:
        alarm = self.template["Resources"]["DocumentIntegrityAlarm"]["Properties"]
        description = " ".join(alarm["AlarmDescription"].split())
        for needle in ("INDEXED has no object", "Knowledge Base", "lifecycle rule", "Nothing was repaired automatically", "document_object_missing",
                       "probableCause", "docs/phase-14-data-governance-runbook.md", "Ghost documents: confirm the cause and repair", "governance delete + Knowledge Base sync"):
            self.assertIn(needle, description)
        runbook = GOVERNANCE_RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("## Ghost documents: confirm the cause and repair", runbook)

    def test_metric_filter_alarm_and_the_cost_statement(self) -> None:
        resources = self.template["Resources"]
        pattern = resources["DocumentIntegrityMetricFilter"]["Properties"]["FilterPattern"]
        self.assertEqual(pattern, '{ $.event = "document_objects_check" && $.findings > 0 }')
        transformation = resources["DocumentIntegrityMetricFilter"]["Properties"]["MetricTransformations"][0]
        self.assertEqual((transformation["MetricName"], transformation["MetricValue"]), ("DocumentIntegrityFindings", "$.findings"))
        alarm = resources["DocumentIntegrityAlarm"]["Properties"]
        self.assertEqual((alarm["MetricName"], alarm["Namespace"], alarm["Threshold"], alarm["TreatMissingData"]), ("DocumentIntegrityFindings", "LegalDesk/Phase14Reconciliation", 1, "notBreaching"))
        self.assertEqual((alarm["Period"], alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]), (900, 2, 1))  # the schedule is every 15 minutes: one miss does not clear it
        self.assertEqual(self.template["Parameters"]["DocumentIntegrityAlarmTopicArn"]["Default"], "")  # console-only unless a topic is passed
        self.assertIn("0.40 USD a month", self.text)
        self.assertEqual(self.template["Conditions"]["DocumentIntegrityAlarmEnabled"], ["EnableDocumentIntegrityAlarm", "true"])

    def test_the_log_line_the_filter_reads_is_the_line_the_lambda_prints(self) -> None:
        report = World([document(1)], [], [THIRTY_DAY_RULE]).run()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rl._emit_object_findings(report)
        check = [json.loads(line) for line in out.getvalue().splitlines() if '"document_objects_check"' in line][0]
        self.assertEqual(check["event"], "document_objects_check")
        self.assertGreater(check["findings"], 0)
        self.assertTrue(isinstance(check["findings"], int))

    def test_the_reconciliation_change_set_is_exactly_the_profile(self) -> None:
        before = preview.load_template((FIXTURES / "phase-14-reconciliation.before-integrity.yaml").read_text(encoding="utf-8"))
        after = preview.load_template((TEMPLATES / "phase-14-reconciliation.yaml").read_text(encoding="utf-8"))
        deployed = {"ReconciliationCodeBucket": "b", "ReconciliationCodeKey": "old", "ReconciliationCodeVersion": "v1", "UploadScopes": "[]", "IngestionScopes": "[]",
                    "KnowledgeBaseId": "k", "DataSourceId": "d", "KnowledgeBaseArn": "a", "BetaTenantId": "t", "MetadataTableName": "m", "MetadataTableArn": "a",
                    "SourceBucketName": "bb", "SourceBucketArn": "a"}
        local = {**deployed, "ReconciliationCodeKey": "new", "ReconciliationCodeVersion": "v2"}
        result = preview.compare(dict(before), dict(after), deployed, local)
        self.assertEqual(preview.check_preview(result, preview.load_profile("reconciliation-object-integrity")), [])
        self.assertEqual({item["id"] for item in result["added"]}, {"DocumentIntegrityAlarm", "DocumentIntegrityMetricFilter"})
        self.assertTrue(all(item["replacement"] == "no" for item in result["modified"]))
        self.assertFalse(any(change["wide"] for change in result["iam"]))


class TheOperatorRunbookExistsTests(unittest.TestCase):
    def test_the_runbook_section_names_the_causes_the_commands_and_the_decision(self) -> None:
        runbook = " ".join(GOVERNANCE_RUNBOOK.read_text(encoding="utf-8").split())
        for needle in (
            "no automatic expiry", "ExpireFictionalOriginals", "CANONICAL_EXPIRY_KEEPS_RECORDS_CONSISTENT", "document_object_missing", "probableCause",
            "s3_lifecycle_expiration", "unknown_", "phase15_ghost_documents_cleanup.py", "start-ingestion-job", "list-knowledge-base-documents",
            "Report only", "DocumentIntegrityFindings",
        ):
            self.assertIn(needle, runbook)


if __name__ == "__main__":
    unittest.main()
