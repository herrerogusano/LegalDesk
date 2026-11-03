"""The ghost-document operator tool: dry-run by default, authorised only through the governance scope, idempotent, bounded, closed output."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from fixture_loader import test_identity  # noqa: E402
from legaldesk.authorization import InMemoryAuthorizationStore  # noqa: E402
from legaldesk.data_governance import OperatorDataGovernanceService  # noqa: E402
from legaldesk.documents import InMemoryDocumentMetadataRepository, InMemoryObjectStorage  # noqa: E402
from legaldesk.domain.models import Document, DocumentStatus, Matter, MalwareScanStatus, User  # noqa: E402
from legaldesk.review_tasks import InMemoryReviewTaskRepository  # noqa: E402
from scripts import phase15_ghost_documents_cleanup as tool  # noqa: E402

TENANT, MATTER = "tnt_phase14_20260928", "mat_phase14_a_20260928"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
GHOST, GHOST_2, HEALTHY, FAILED_RECORD = (f"6f1d0c1e-0000-4000-8000-{i:012d}" for i in (1, 2, 3, 4))


def key(document_id: str) -> str:
    return f"tenants/{TENANT}/matters/{MATTER}/documents/{document_id}/original.txt"


def document(document_id: str, *, status=DocumentStatus.INDEXED, age_days=36, s3_key: str | None = None) -> Document:
    return Document(
        document_id=document_id, matter_id=MATTER, tenant_id=TENANT, name="SECRET-NAME.txt", s3_key=s3_key or key(document_id), media_type="text/plain",
        jurisdiction="ES", document_date="2026-09-28", confidentiality="internal", status=status, malware_scan_status=MalwareScanStatus.CLEAN,
        uploaded_at=NOW - timedelta(days=age_days),
    )


class World:
    def __init__(self, *, roles=frozenset({"data_steward"}), member=True, tenant=TENANT):
        self.users = {"idp|op": User("user-op", "idp|op", frozenset({tenant}), roles)}
        matter = Matter(MATTER, TENANT, "Matter", frozenset({"user-op"} if member else set()))
        self.auth = InMemoryAuthorizationStore(users_by_subject=self.users, matters_by_id={MATTER: matter})
        self.records = InMemoryDocumentMetadataRepository()
        self.storage = InMemoryObjectStorage()
        for item in (document(GHOST), document(GHOST_2), document(HEALTHY), document(FAILED_RECORD, status=DocumentStatus.FAILED)):
            self.records.save(item)
        # the sidecars exist for the healthy one only; GHOST, GHOST_2 and FAILED_RECORD lost their objects
        self.storage.objects[key(HEALTHY)] = b"x"
        self.storage.objects[key(HEALTHY) + ".metadata.json"] = b"{}"
        self.service = OperatorDataGovernanceService(metadata_repository=self.records, object_storage=self.storage, review_repository=InMemoryReviewTaskRepository())
        self.verified: list[str] = []

    def backends(self, *, token_ok=True) -> tool.Backends:
        def verify(token: str):
            self.verified.append(token)
            if not token_ok:
                raise ValueError("bad token")
            return test_identity("idp|op")

        return tool.Backends(
            verify_token=verify, authorization_store=self.auth, metadata_repository=self.records, object_storage=self.storage, service=self.service,
            knowledge_base_id="AQWCFL7239", data_source_id="XIA81RBIPG", clock=lambda: NOW,
        )

    def run(self, *argv, token="TOKEN", **kwargs):
        out = io.StringIO()
        environ = {tool.TOKEN_ENV: token} if token else {}
        with contextlib.redirect_stdout(out):
            code = tool.main(list(argv), backends=self.backends(**kwargs), environ=environ)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]


def delete_args(*ids, execute=False):
    args = ["delete", "--tenant", TENANT, "--matter", MATTER]
    for item in ids:
        args += ["--document-id", item]
    return args + (["--execute"] if execute else [])


class DryRunIsTheDefaultTests(unittest.TestCase):
    def test_without_execute_nothing_changes_and_each_id_gets_a_closed_status(self) -> None:
        world = World()
        before = (dict(world.records.documents), dict(world.storage.objects))
        code, lines = world.run(*delete_args(GHOST, GHOST_2, FAILED_RECORD))
        self.assertEqual(code, 0)
        statuses = [(line["documentId"], line["inspection"], line["outcome"]) for line in lines if "documentId" in line]
        self.assertEqual(statuses, [(GHOST, "ghost_indexed", "would_delete"), (GHOST_2, "ghost_indexed", "would_delete"), (FAILED_RECORD, "ghost_record", "would_delete")])
        self.assertEqual((dict(world.records.documents), dict(world.storage.objects)), before)
        self.assertFalse(any("commands" in line for line in lines))  # follow-ups only after a real delete

    def test_the_output_has_no_key_name_bucket_token_or_tenant_record(self) -> None:
        _, lines = World().run(*delete_args(GHOST), token="SECRET-TOKEN-VALUE")
        text = json.dumps(lines)
        for forbidden in ("SECRET-NAME", "tenants/", "SECRET-TOKEN-VALUE", "original.txt"):
            self.assertNotIn(forbidden, text)


class ExecuteDeletesThroughTheGovernanceServiceTests(unittest.TestCase):
    def test_execute_removes_record_and_objects_and_prints_the_exact_follow_up(self) -> None:
        world = World()
        code, lines = world.run(*delete_args(GHOST, GHOST_2, execute=True))
        self.assertEqual(code, 0)
        self.assertEqual([(l["documentId"], l["outcome"]) for l in lines if "documentId" in l], [(GHOST, "deleted"), (GHOST_2, "deleted")])
        self.assertEqual({d.document_id for d in world.records.documents.values()}, {HEALTHY, FAILED_RECORD})
        self.assertIn(key(HEALTHY), world.storage.objects)  # the healthy document is untouched
        follow = next(l for l in lines if "commands" in l)["commands"]
        self.assertEqual(follow[0], "aws bedrock-agent start-ingestion-job --knowledge-base-id AQWCFL7239 --data-source-id XIA81RBIPG")
        self.assertEqual(len(follow), 3)
        for command, document_id in zip(follow[1:], (GHOST, GHOST_2)):
            self.assertIn("list-knowledge-base-documents --knowledge-base-id AQWCFL7239 --data-source-id XIA81RBIPG", command)
            self.assertIn(document_id, command)

    def test_a_second_run_is_idempotent(self) -> None:
        world = World()
        world.run(*delete_args(GHOST, execute=True))
        code, lines = world.run(*delete_args(GHOST, execute=True))
        self.assertEqual(code, 0)
        self.assertEqual([(l["inspection"], l["outcome"]) for l in lines if "documentId" in l], [("already_absent", "already_absent")])
        self.assertTrue(any("commands" in l for l in lines))  # the KB follow-up is repeated: it may still be pending

    def test_a_document_whose_object_exists_is_refused_even_with_execute(self) -> None:
        world = World()
        code, lines = world.run(*delete_args(HEALTHY, GHOST, execute=True))
        self.assertEqual(code, 1)
        outcomes = {l["documentId"]: l["outcome"] for l in lines if "documentId" in l}
        self.assertEqual(outcomes, {HEALTHY: "refused_object_present", GHOST: "deleted"})
        self.assertIn(HEALTHY, {d.document_id for d in world.records.documents.values()})
        self.assertIn(key(HEALTHY), world.storage.objects)

    def test_the_failed_records_without_objects_are_deleted_the_same_way(self) -> None:
        world = World()
        code, lines = world.run(*delete_args(FAILED_RECORD, execute=True))
        self.assertEqual((code, [l["outcome"] for l in lines if "documentId" in l]), (0, ["deleted"]))
        self.assertNotIn(FAILED_RECORD, {d.document_id for d in world.records.documents.values()})

    def test_a_failure_of_the_service_is_reported_with_closed_targets_and_exit_one(self) -> None:
        world = World()
        world.storage.fail_delete = True
        code, lines = world.run(*delete_args(GHOST, execute=True))
        self.assertEqual(code, 1)
        failed = next(l for l in lines if "documentId" in l)
        self.assertEqual((failed["outcome"], failed["failures"]), ("failed", ["object_cleanup"]))
        self.assertIn(GHOST, {d.document_id for d in world.records.documents.values()})  # the record stays so the repair can be retried


class TheOperatorScopeIsNeverBypassedTests(unittest.TestCase):
    def test_a_member_without_an_operator_role_is_refused_and_nothing_is_touched(self) -> None:
        """The synthetic test user has roles ["member"]: it does not qualify."""

        world = World(roles=frozenset({"member"}))
        code, lines = world.run(*delete_args(GHOST, execute=True))
        self.assertEqual(code, 2)
        self.assertEqual(lines[0]["outcome"], "operator_scope_denied")
        self.assertEqual(lines[0]["reason"], "not_an_operator_of_this_matter")
        self.assertIn(GHOST, {d.document_id for d in world.records.documents.values()})

    def test_an_operator_who_is_not_a_member_of_the_matter_is_refused(self) -> None:
        code, lines = World(member=False).run(*delete_args(GHOST, execute=True))
        self.assertEqual((code, lines[0]["outcome"]), (2, "operator_scope_denied"))

    def test_an_unverified_token_is_refused_without_echoing_why(self) -> None:
        code, lines = World().run(*delete_args(GHOST, execute=True), token_ok=False)
        self.assertEqual((code, lines[0]["reason"]), (2, "token_not_verified"))
        self.assertNotIn("bad token", json.dumps(lines))

    def test_the_tenant_argument_is_compared_never_trusted(self) -> None:
        world = World()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(["delete", "--tenant", "tnt_other", "--matter", MATTER, "--document-id", GHOST, "--execute"],
                             backends=world.backends(), environ={tool.TOKEN_ENV: "T"})
        self.assertEqual((code, json.loads(out.getvalue().splitlines()[0])["outcome"]), (2, "tenant_mismatch"))
        self.assertIn(GHOST, {d.document_id for d in world.records.documents.values()})

    def test_the_token_comes_only_from_the_environment(self) -> None:
        with self.assertRaises(SystemExit):
            World().run(*delete_args(GHOST), token="")
        parser_help = tool._arguments.__code__.co_consts
        self.assertFalse(any(isinstance(c, str) and "--token" in c for c in parser_help))


class TheInputsAreBoundedTests(unittest.TestCase):
    def test_bad_ids_duplicates_and_too_many_are_refused_before_any_call(self) -> None:
        world = World()
        for argv in (delete_args("not-a-uuid"), delete_args(GHOST, GHOST), delete_args(*[f"6f1d0c1e-0000-4000-8000-{i:012d}" for i in range(20, 31)])):
            with self.subTest(argv=argv[4:6]):
                with self.assertRaises(SystemExit):
                    world.run(*argv)
        self.assertEqual(world.verified, [])  # nothing reached the verifier

    def test_the_command_needs_at_least_one_id(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            World().run("delete", "--tenant", TENANT, "--matter", MATTER)


class AuditListsTheGhostsReadOnlyTests(unittest.TestCase):
    def test_audit_reports_every_record_without_an_object_with_status_and_age(self) -> None:
        world = World()
        before = (dict(world.records.documents), dict(world.storage.objects))
        code, lines = world.run("audit", "--tenant", TENANT, "--matter", MATTER)
        self.assertEqual(code, 0)
        found = {l["documentId"]: (l["recordStatus"], l["inspection"], l["ageDays"]) for l in lines if "documentId" in l}
        self.assertEqual(found, {GHOST: ("INDEXED", "ghost_indexed", 36), GHOST_2: ("INDEXED", "ghost_indexed", 36), FAILED_RECORD: ("FAILED", "ghost_record", 36)})
        self.assertEqual(lines[-1], {"summary": "audit", "ghosts": 3})
        self.assertEqual((dict(world.records.documents), dict(world.storage.objects)), before)

    def test_audit_refuses_to_guess_beyond_its_bound(self) -> None:
        world = World()
        for index in range(100, 100 + tool.AUDIT_LIMIT):
            world.records.save(document(f"6f1d0c1e-0000-4000-8000-{index:012d}"))
        with self.assertRaises(Exception):
            world.run("audit", "--tenant", TENANT, "--matter", MATTER)


if __name__ == "__main__":
    unittest.main()
