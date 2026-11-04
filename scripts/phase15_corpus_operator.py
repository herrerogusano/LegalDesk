"""Prepare the synthetic corpus through the real HTTP upload pipeline. Dry-run by default.

One selected document per execution; no automatic retry, polling, model invocation or record seeding.
The durable journal stops ambiguous uploads from silently becoming duplicate paid work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # never forward cookies, CSRF or a presigned URL to a redirect target


def file_plan(manifest_path: Path) -> list[dict[str, Any]]:
    sys.path.insert(0, str(ROOT / "backend/src"))
    from legaldesk.documents import MAX_DOCUMENT_BYTES
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = manifest["documents"]
    frozen = json.loads((ROOT / "evals/phase15_golden_dataset.json").read_text(encoding="utf-8"))
    known = {d["documentId"]: d for d in frozen["world"]["documents"]}
    if len(documents) != 27 or {d["goldenId"] for d in documents} != set(known):
        raise ValueError("expected exactly the 27 frozen synthetic corpus files")
    result = []
    for document in documents:
        if document["matterId"] != known[document["goldenId"]]["matterId"]:
            raise ValueError("unexpected corpus scope")
        name = document["name"]
        if Path(name).name != name or not name.endswith(".pdf"):
            raise ValueError("invalid corpus filename")
        folder = ROOT / "tests/fixtures/idp/pdfs" if document["source"] == "tests/fixtures/idp/pdfs" else manifest_path.parent
        path = folder / name
        data = path.read_bytes()
        if not 1 <= len(data) <= MAX_DOCUMENT_BYTES or hashlib.sha256(data).hexdigest() != document["sha256"]:
            raise ValueError("corpus hash or size mismatch")
        result.append({**document, "path": path, "size": len(data)})
    return result


def read_journal(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def record(path: Path, **entry: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def upload_one(document: Mapping[str, Any], *, matter: str, journal: Path,
               api: Callable[..., Any], put: Callable[..., Any]) -> str:
    prior = [e for e in read_journal(journal) if e["goldenId"] == document["goldenId"]]
    if prior:
        if any(e["matterId"] != matter or e["sha256"] != document["sha256"] for e in prior):
            raise ValueError("journal scope/hash mismatch")
        if prior[-1]["state"] == "confirmed":
            return prior[-1]["documentId"]  # completed upload is an idempotent no-op
        raise ValueError("ambiguous upload: inspect its recorded id; do not authorize a duplicate")
    common = {"goldenId": document["goldenId"], "matterId": matter, "sha256": document["sha256"]}
    # Record intent BEFORE authorization: a lost response cannot lead to a second upload on rerun.
    record(journal, **common, state="authorizing")
    route = f"/api/matters/{matter}/documents"
    response = api("POST", route + "/upload-authorizations", {
        "filename": document["name"], "mediaType": "application/pdf", "fileSizeBytes": document["size"],
        "jurisdiction": "fictional", "documentDate": "2099-01-01", "confidentiality": "fictional-internal"})
    document_id = response["document"]["documentId"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", document_id):
        raise ValueError("invalid assigned document id")
    record(journal, **common, state="authorized", documentId=document_id)
    put(response["uploadUrl"], document["path"].read_bytes(), response["headers"])
    record(journal, **common, state="uploaded", documentId=document_id)
    api("POST", route + f"/{document_id}/confirm", {})
    record(journal, **common, state="confirmed", documentId=document_id)
    return document_id


def create_matters(store: Any, *, subject: str, operator_subject: str, reference_matter: str,
                   matter: str, other: str, execute: bool) -> list[str]:
    """Provision only two new authorization profiles; never mutate an existing user or document record."""
    sys.path.insert(0, str(ROOT / "backend/src"))
    from legaldesk.authorization import authorization_profile_sort_key
    user, operator = store.get_user_by_subject(subject), store.get_user_by_subject(operator_subject)
    reference = store.get_matter(reference_matter)
    if (not user or not operator or not reference or reference.tenant_id not in user.tenant_ids
            or reference.tenant_id not in operator.tenant_ids or user.user_id not in reference.authorized_user_ids
            or not set(operator.roles).intersection({"operator", "admin", "data_steward"})
            or user.user_id == operator.user_id):
        raise ValueError("verified synthetic member and distinct operator authorization required")
    if matter == other or any(not re.fullmatch(r"mat_phase15_[a-z0-9_]{3,64}", i) for i in (matter, other)):
        raise ValueError("two dedicated mat_phase15_ ids required")
    items = []
    for selected, users in ((matter, [user.user_id, operator.user_id]), (other, [operator.user_id])):
        item = {"pk": f"AUTH#MATTER#{selected}", "sk": authorization_profile_sort_key(), "entityType": "Matter",
                "matterId": selected, "tenantId": reference.tenant_id, "name": "Phase 15 synthetic corpus",
                "authorizedUserIds": users, "status": "active"}
        existing = store.get_matter(selected)
        if existing:
            if (existing.tenant_id != reference.tenant_id or set(existing.authorized_user_ids) != set(users)
                    or str(existing.status) != "active"):
                raise ValueError("existing matter does not match; no mutation allowed")
        items.append((item, existing is None))
    for item, absent in items:  # all profiles checked before the first write
        if execute and absent:
            store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    return [matter, other]


def transport(base: str, cookie: str, csrf: str) -> tuple[Callable[..., Any], Callable[..., Any]]:
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError("base URL must be an exact HTTPS origin without a trailing slash")
    opener = build_opener(NoRedirect)

    def api(method: str, route: str, data: Any = None) -> Any:
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": base, "Content-Type": "application/json"}
        body = json.dumps(data).encode() if data is not None else None
        with opener.open(Request(base + route, data=body, headers=headers, method=method), timeout=30) as response:
            return json.loads(response.read(1_000_000))

    def put(url: str, data: bytes, headers: Mapping[str, str]) -> None:
        target = urlsplit(url)
        if (target.scheme != "https" or not target.hostname or not target.hostname.endswith(".amazonaws.com")
                or ".s3" not in target.hostname or target.username or target.password):
            raise ValueError("upload URL must be an HTTPS S3 presigned URL")
        with opener.open(Request(url, data=data, headers=dict(headers), method="PUT"), timeout=60):
            pass  # API cookie/CSRF are never sent to S3
    return api, put


def cleanup_matter(matter: str, token: str) -> Mapping[str, Any]:
    sys.path.insert(0, str(ROOT / "backend/src"))
    from legaldesk.application import AWSResourceConfig, build_aws_composition
    from legaldesk.agent_jobs import DynamoDBChatJobStore
    from legaldesk.data_governance import OperatorDataGovernanceService, authorize_operator_scope
    config = AWSResourceConfig.from_environment()
    composition = build_aws_composition(allow_aws=True, config=config)
    identity = composition.identity_verifier.verify_authorization_header(f"Bearer {token}")
    scope = authorize_operator_scope(identity, matter, composition.authorization_store)
    service = OperatorDataGovernanceService(metadata_repository=composition.metadata_repository,
        object_storage=composition.object_storage, review_repository=composition.review_repository,
        state_store=composition.state_store, job_store=DynamoDBChatJobStore(config.metadata_table_name))
    report = service.delete_matter(scope)
    return {**report.as_dict(), "ok": report.ok}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create-matters", "upload", "status", "ingest", "map", "cleanup"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--other-matter-id")
    parser.add_argument("--golden-id")
    parser.add_argument("--base-url")
    parser.add_argument("--table")
    parser.add_argument("--subject")
    parser.add_argument("--operator-subject")
    parser.add_argument("--reference-matter")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"mat_phase15_[a-z0-9_]{3,64}", args.matter_id):
        parser.error("dedicated synthetic matter id required")
    if args.command == "cleanup":
        if not args.execute:
            print(json.dumps({"dryRun": True, "operation": "governance_delete_matter", "matterId": args.matter_id}))
            return 0
        token = os.environ.get("LEGALDESK_OPERATOR_ACCESS_TOKEN", "")
        if not token:
            parser.error("cleanup requires LEGALDESK_OPERATOR_ACCESS_TOKEN")
        result = cleanup_matter(args.matter_id, token)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "create-matters":
        if not all((args.table, args.subject, args.operator_subject, args.reference_matter, args.other_matter_id)):
            parser.error("create-matters needs table, both subjects, reference and other matter")
        if not args.execute:
            print(json.dumps({"dryRun": True, "newAuthorizationProfiles": [args.matter_id, args.other_matter_id], "documentWrites": 0}))
            return 0
        import boto3
        from botocore.config import Config
        sys.path.insert(0, str(ROOT / "backend/src"))
        from legaldesk.authorization import Boto3DynamoAuthorizationStore
        table = boto3.resource("dynamodb", region_name="eu-west-1", config=Config(retries={"total_max_attempts": 1})).Table(args.table)
        store = Boto3DynamoAuthorizationStore(args.table, table=table)
        print(json.dumps({"matters": create_matters(store, subject=args.subject, operator_subject=args.operator_subject,
              reference_matter=args.reference_matter, matter=args.matter_id, other=args.other_matter_id, execute=True)}))
        return 0
    if not args.manifest or not args.journal:
        parser.error("manifest and journal required")
    documents = file_plan(args.manifest)
    entries = read_journal(args.journal)
    if args.command == "map":
        mapping = {e["goldenId"]: e["documentId"] for e in entries if e["state"] == "confirmed"}
        if len(mapping) != 27 or len(set(mapping.values())) != 27:
            raise ValueError("all 27 confirmed uploads are required; lifecycle verification is separate")
        print(json.dumps(mapping, indent=2, sort_keys=True))
        return 0
    if args.command == "upload":
        documents = [d for d in documents if d["goldenId"] == args.golden_id]
        if len(documents) != 1:
            parser.error("upload needs one manifest golden id")
    if not args.execute:
        print(json.dumps({"dryRun": True, "command": args.command, "documents": len(documents), "retries": 0}))
        return 0
    cookie, csrf = os.environ.get("LEGALDESK_CORPUS_COOKIE", ""), os.environ.get("LEGALDESK_CORPUS_CSRF", "")
    if not cookie or not csrf or not args.base_url:
        parser.error("execute needs base URL and LEGALDESK_CORPUS_COOKIE / LEGALDESK_CORPUS_CSRF")
    api, put = transport(args.base_url, cookie, csrf)
    if args.command == "upload":
        doc = documents[0]
        matter = args.other_matter_id if doc["matterId"] == "mat_glacier" else args.matter_id
        if not matter:
            parser.error("other matter required for Glacier document")
        print(json.dumps({"documentId": upload_one(doc, matter=matter, journal=args.journal, api=api, put=put)}))
    else:
        indexed = {e["goldenId"]: e for e in entries if e["state"] == "confirmed" and e["matterId"] == args.matter_id}
        if not indexed or len(indexed) > 27:
            raise ValueError("bounded confirmed journal required")
        rows = [api("GET", f"/api/matters/{args.matter_id}/documents/{e['documentId']}")["document"] for e in indexed.values()]
        if args.command == "status":
            print(json.dumps([{k: row[k] for k in ("documentId", "status", "malwareScanStatus")} for row in rows], indent=2))
        else:
            ready = sorted(r["documentId"] for r in rows if r["status"] == "UPLOADED" and r["malwareScanStatus"] == "CLEAN")
            for start in range(0, len(ready), 20):
                ids = ready[start:start + 20]
                key = "phase15-corpus-" + hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:32]
                result = api("POST", f"/api/matters/{args.matter_id}/ingestions", {"documentIds": ids, "idempotencyKey": key})
                record(args.journal, goldenId="ingestion", matterId=args.matter_id, sha256=key, state="ingestion", operation=result)
                print(json.dumps({"ingestion": result}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Exceptions from HTTP libraries can contain cookies or presigned URLs. Never print them.
        print(json.dumps({"error": "corpus_operation_failed", "action": "inspect journal and lifecycle; do not retry an ambiguous upload"}))
        raise SystemExit(1)
