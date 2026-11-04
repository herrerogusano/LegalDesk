import hashlib
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import phase15_corpus_operator as op
sys.path.insert(0, str(op.ROOT / "backend/src"))
from legaldesk.authorization import Boto3DynamoAuthorizationStore


class CorpusOperatorTests(unittest.TestCase):
    def test_existing_primary_map_is_complete_hash_bound_and_accepted_by_golden_loader(self):
        from evals.phase15_golden_eval import load_dataset, prepare_corpus, build_world, validate_document_map
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = load_dataset()[0]
            prepare_corpus(dataset, root, pdf_writer=lambda doc, path: path.write_bytes(b"synthetic PDF"))
            docs = op.file_plan(root / "corpus-manifest.json")
            entries = [{"goldenId": d["goldenId"], "matterId": op.EXISTING_PRIMARY_MATTER,
                        "sha256": d["sha256"], "state": "confirmed", "documentId": f"real-{i}"}
                       for i, d in enumerate(docs) if d["matterId"] != "mat_glacier"]
            mapping = op.document_map(docs, entries, matter=op.EXISTING_PRIMARY_MATTER, other=None)
            self.assertEqual(len(mapping), 26)
            self.assertNotIn("doc-glacier-contract-01", mapping)
            self.assertIn("doc-failed-upload", mapping)
            self.assertEqual(len(validate_document_map(build_world(dataset), mapping)), 26)
            for change in ("missing", "hash", "scope", "duplicate", "ambiguous"):
                bad = [dict(e) for e in entries]
                if change == "missing": bad.pop()
                elif change == "hash": bad[0]["sha256"] = "0" * 64
                elif change == "scope": bad[0]["matterId"] = "mat_phase14_a_20260928"
                elif change == "duplicate": bad[0]["documentId"] = bad[1]["documentId"]
                else: bad[-1]["state"] = "uploaded"
                with self.subTest(change=change), self.assertRaises(ValueError):
                    op.document_map(docs, bad, matter=op.EXISTING_PRIMARY_MATTER, other=None)

    def test_existing_primary_upload_dry_run_and_glacier_refusal_before_transport(self):
        from evals.phase15_golden_eval import load_dataset, prepare_corpus
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_corpus(load_dataset()[0], root, pdf_writer=lambda doc, path: path.write_bytes(b"synthetic PDF"))
            args = ["upload", "--matter-id", op.EXISTING_PRIMARY_MATTER, "--manifest", str(root / "corpus-manifest.json"),
                    "--journal", str(root / "journal.jsonl"), "--golden-id", "doc-failed-upload"]
            with patch.object(op, "transport", side_effect=AssertionError("HTTP")), patch("builtins.print"):
                self.assertEqual(op.main(args), 0)
                with self.assertRaises(SystemExit):
                    op.main(args[:-1] + ["doc-glacier-contract-01", "--execute"])
                with self.assertRaises(SystemExit):
                    op.main(["create-matters", "--matter-id", op.EXISTING_PRIMARY_MATTER])
                for matter in ("mat_phase14_a_20260928", "mat_arbitrary", op.EXISTING_PRIMARY_MATTER + "_copy"):
                    with self.subTest(matter=matter), self.assertRaises(SystemExit):
                        op.main(["cleanup", "--matter-id", matter])

    def test_full_manifest_is_validated_before_network_and_tampering_is_rejected(self):
        from evals.phase15_golden_eval import load_dataset, prepare_corpus
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_corpus(load_dataset()[0], root, pdf_writer=lambda doc, path: path.write_bytes(b"synthetic PDF"))
            manifest = root / "corpus-manifest.json"
            self.assertEqual(len(op.file_plan(manifest)), 27)
            (root / "scan-upload-failed.pdf").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash or size"):
                op.file_plan(manifest)

    def test_upload_crosses_real_api_contract_and_is_idempotent_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, journal = root / "synthetic.pdf", root / "journal.jsonl"
            path.write_bytes(b"synthetic")
            doc = {"goldenId": "doc-test", "name": path.name, "path": path, "size": 9,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            api, put = Mock(), Mock()
            api.return_value = {"document": {"documentId": "assigned-id"}, "uploadUrl": "https://s3.example.invalid", "headers": {"Content-Type": "application/pdf"}}
            self.assertEqual(op.upload_one(doc, matter="mat_phase15_test", journal=journal, api=api, put=put), "assigned-id")
            self.assertEqual(api.call_args_list[0].args[:2], ("POST", "/api/matters/mat_phase15_test/documents/upload-authorizations"))
            self.assertEqual(api.call_args_list[1].args, ("POST", "/api/matters/mat_phase15_test/documents/assigned-id/confirm", {}))
            put.assert_called_once()
            op.upload_one(doc, matter="mat_phase15_test", journal=journal, api=api, put=put)
            self.assertEqual(api.call_count, 2)
            self.assertNotIn("s3.example", journal.read_text())  # no signed URL, cookie or token in checkpoint

    def test_lost_authorization_or_put_response_never_creates_duplicate_upload(self):
        for fail_at in ("authorization", "put"):
            with tempfile.TemporaryDirectory() as tmp:
                path, journal = Path(tmp) / "a.pdf", Path(tmp) / "j.jsonl"
                path.write_bytes(b"x")
                doc = {"goldenId": "doc-x", "name": "a.pdf", "sha256": "a" * 64, "size": 1, "path": path}
                api, put = Mock(), Mock()
                api.return_value = {"document": {"documentId": "id"}, "uploadUrl": "secret", "headers": {}}
                (api if fail_at == "authorization" else put).side_effect = TimeoutError("secret")
                with self.assertRaises(TimeoutError):
                    op.upload_one(doc, matter="mat_phase15_test", journal=journal, api=api, put=put)
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    op.upload_one(doc, matter="mat_phase15_test", journal=journal, api=api, put=put)
                self.assertEqual(api.call_count, 1)

    def test_matter_creation_authorizes_member_only_in_primary_and_never_overwrites(self):
        items = {
            "AUTH#USER#member": {"entityType": "User", "userId": "u-member", "verifiedSubject": "member", "tenantIds": ["tnt_synthetic"], "roles": ["member"]},
            "AUTH#USER#operator": {"entityType": "User", "userId": "u-operator", "verifiedSubject": "operator", "tenantIds": ["tnt_synthetic"], "roles": ["operator"]},
            "AUTH#MATTER#mat_existing": {"entityType": "Matter", "matterId": "mat_existing", "tenantId": "tnt_synthetic", "name": "Synthetic", "authorizedUserIds": ["u-member"], "status": "active"},
        }
        table = Mock()
        table.get_item.side_effect = lambda **kw: {"Item": items[kw["Key"]["pk"]]} if kw["Key"]["pk"] in items else {}
        def write(**kw):
            self.assertEqual(kw["ConditionExpression"], "attribute_not_exists(pk)")
            items[kw["Item"]["pk"]] = kw["Item"]
        table.put_item.side_effect = write
        store = Boto3DynamoAuthorizationStore("table", table=table)
        args = dict(subject="member", operator_subject="operator", reference_matter="mat_existing",
                    matter="mat_phase15_main", other="mat_phase15_other")
        op.create_matters(store, execute=False, **args)
        table.put_item.assert_not_called()
        op.create_matters(store, execute=True, **args)
        self.assertEqual(table.put_item.call_count, 2)
        self.assertEqual(items["AUTH#MATTER#mat_phase15_main"]["authorizedUserIds"], ["u-member", "u-operator"])
        self.assertEqual(items["AUTH#MATTER#mat_phase15_other"]["authorizedUserIds"], ["u-operator"])
        op.create_matters(store, execute=True, **args)
        self.assertEqual(table.put_item.call_count, 2)
        items["AUTH#MATTER#mat_phase15_other"]["authorizedUserIds"].append("u-member")
        with self.assertRaises(ValueError):
            op.create_matters(store, execute=True, **args)
        self.assertEqual(table.put_item.call_count, 2)

    def test_dry_run_never_builds_cloud_or_http_clients(self):
        with patch("boto3.resource", side_effect=AssertionError("network")), patch.object(op, "transport", side_effect=AssertionError("HTTP")), patch("builtins.print"):
            self.assertEqual(op.main(["create-matters", "--matter-id", "mat_phase15_main", "--other-matter-id", "mat_phase15_other",
                                      "--table", "t", "--subject", "member", "--operator-subject", "operator", "--reference-matter", "mat_existing"]), 0)

    def test_transport_refuses_non_https_origin_and_s3_credentials_are_separate(self):
        for base in ("http://example.com", "https://example.com/", "https://user:pass@example.com", "https://example.com?x=secret"):
            with self.assertRaises(ValueError):
                op.transport(base, "cookie", "csrf")
        api, put = op.transport("https://example.com", "secret-cookie", "secret-csrf")
        for url in ("https://evil.example.com", "http://bucket.s3.amazonaws.com"):
            with self.assertRaises(ValueError):
                put(url, b"x", {})

    def test_http_transport_sends_session_only_to_api_and_signed_headers_only_to_s3(self):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=SimpleNamespace(read=lambda size: b"{}"))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(op, "build_opener", return_value=opener):
            api, put = op.transport("https://example.com", "private-cookie", "private-csrf")
            api("POST", "/api/matters/m/documents/upload-authorizations", {})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Cookie"), "private-cookie")
            self.assertEqual(request.get_header("Origin"), "https://example.com")
            self.assertEqual(request.get_header("X-csrf-token"), "private-csrf")
            put("https://bucket.s3.eu-west-1.amazonaws.com/key?signature=private", b"pdf", {"Content-Type": "application/pdf"})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "PUT")
            self.assertIsNone(request.get_header("Cookie"))
            self.assertIsNone(request.get_header("X-csrf-token"))

    def test_cleanup_uses_verified_operator_scope_and_includes_job_and_state_cleanup(self):
        composition = Mock()
        config = SimpleNamespace(metadata_table_name="table")
        with patch("legaldesk.application.AWSResourceConfig.from_environment", return_value=config), \
             patch("legaldesk.application.build_aws_composition", return_value=composition), \
             patch("legaldesk.data_governance.authorize_operator_scope") as authorize, \
             patch("legaldesk.data_governance.OperatorDataGovernanceService") as service, \
             patch("legaldesk.agent_jobs.DynamoDBChatJobStore") as jobs:
            report = service.return_value.delete_matter.return_value
            report.as_dict.return_value = {"failures": [], "indexCleanupPending": True}
            report.ok = True
            result = op.cleanup_matter("mat_phase15_test", "private-placeholder")
        composition.identity_verifier.verify_authorization_header.assert_called_once_with("Bearer private-placeholder")
        authorize.assert_called_once_with(composition.identity_verifier.verify_authorization_header.return_value,
                                          "mat_phase15_test", composition.authorization_store)
        self.assertIs(service.call_args.kwargs["state_store"], composition.state_store)
        self.assertIs(service.call_args.kwargs["job_store"], jobs.return_value)
        self.assertTrue(result["indexCleanupPending"])
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
