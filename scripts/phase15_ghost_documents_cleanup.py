"""Operator tool: find and remove "ghost" documents (T9 step 12, 2026-11-03).

A ghost document is a metadata record (usually INDEXED, still in the Knowledge Base) whose canonical object no longer exists in the
source bucket, so every search over its matter fails closed. The repair is the data-governance delete
(``OperatorDataGovernanceService.delete_document``: tombstone, objects, record), followed by a Knowledge Base sync.

Two commands, both READ-ONLY unless ``--execute`` is given:

``audit``   list the records of one matter whose canonical object is missing (closed statuses and opaque ids only);
``delete``  for the document ids you name: show what would happen (default) or do it (``--execute``).

Safety properties:

* the operator scope is built by ``authorize_operator_scope`` from a VERIFIED access token (``LEGALDESK_OPERATOR_ACCESS_TOKEN``, never an
  argument): the user must exist in the authorization store with a role in ``OPERATOR_ROLES`` and belong to the matter. Nothing here can
  build a scope without that, and the tool never writes the authorization store;
* the tenant is not an input of authority: ``--tenant`` is only compared with the tenant the scope resolved to;
* a document whose object EXISTS is refused (``refused_object_present``), so a mistyped id cannot delete a healthy document;
* idempotent: an id with no record is ``already_absent`` and succeeds;
* bounded: at most 10 ids per run and 100 records per audit;
* output is closed statuses and the ids you passed (opaque UUIDs): no key, bucket, token, tenant record content or document name.

Not run by CI. See ``docs/phase-14-data-governance-runbook.md`` (section "Ghost documents: confirm the cause and repair").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
for relative in ("backend/src", "agent/src"):
    if str(ROOT / relative) not in sys.path:
        sys.path.insert(0, str(ROOT / relative))

from legaldesk.authorization import AuthorizationDenied, VerifiedIdentity  # noqa: E402
from legaldesk.data_governance import (  # noqa: E402
    GovernanceAuthorizationError,
    GovernanceError,
    OperatorDataGovernanceService,
    OperatorScope,
    authorize_operator_scope,
)
from legaldesk.domain.models import DocumentStatus, MalwareScanStatus  # noqa: E402

TOKEN_ENV = "LEGALDESK_OPERATOR_ACCESS_TOKEN"
MAX_IDS = 10
AUDIT_LIMIT = 100
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# Closed vocabularies of the output.
INSPECTION = ("ghost_indexed", "ghost_record", "refused_object_present", "already_absent", "binding_invalid", "check_failed")
OUTCOME = ("would_delete", "deleted", "failed", "refused_object_present", "already_absent", "binding_invalid", "check_failed")
FAILURE_TARGETS = ("document_binding", "metadata_tombstone", "object_cleanup", "metadata_cleanup")


@dataclass(frozen=True)
class Backends:
    """What the tool needs from the outside; injected so the logic is testable without AWS."""

    verify_token: Callable[[str], VerifiedIdentity]
    authorization_store: Any
    metadata_repository: Any
    object_storage: Any
    service: OperatorDataGovernanceService
    knowledge_base_id: str
    data_source_id: str
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _emit(**fields: object) -> None:
    print(json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str), flush=True)


def _expected_key(document: Any, tenant_id: str, matter_id: str) -> str | None:
    """The canonical key the governance service itself will use; ``None`` if the record's key is not that."""

    extension = {"application/pdf": ".pdf", "text/plain": ".txt"}.get(document.media_type)
    if extension is None:
        return None
    expected = f"tenants/{tenant_id}/matters/{matter_id}/documents/{document.document_id}/original{extension}"
    return expected if document.s3_key == expected else None


def inspect_document(backends: Backends, scope: OperatorScope, document_id: str) -> str:
    """One closed status for one id. Read-only: a keyed GetItem and one existence check."""

    context = scope.context
    try:
        document = backends.metadata_repository.get_for_scope(
            tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id
        )
    except Exception:
        return "check_failed"
    if document is None:
        return "already_absent"
    key = _expected_key(document, context.tenant_id, context.matter_id)
    if key is None:
        return "binding_invalid"
    try:
        present = backends.object_storage.object_exists(key=key)
    except Exception:
        return "check_failed"
    if present:
        return "refused_object_present"
    indexed = document.status is DocumentStatus.INDEXED and document.malware_scan_status is MalwareScanStatus.CLEAN
    return "ghost_indexed" if indexed else "ghost_record"


def audit(backends: Backends, scope: OperatorScope) -> list[dict[str, object]]:
    """Records of the matter (one bounded keyed Query) whose canonical object is missing."""

    context = scope.context
    documents = tuple(backends.metadata_repository.list_for_scope(
        tenant_id=context.tenant_id, matter_id=context.matter_id, limit=AUDIT_LIMIT + 1,
    ))
    if len(documents) > AUDIT_LIMIT:
        raise GovernanceError("matter has more records than the audit bound")
    now = backends.clock()
    findings: list[dict[str, object]] = []
    for document in documents:
        status = inspect_document(backends, scope, document.document_id)
        if status in {"ghost_indexed", "ghost_record", "binding_invalid", "check_failed"}:
            try:
                age = max(0, (now - document.uploaded_at).days)
            except Exception:
                age = None
            findings.append({"documentId": document.document_id, "recordStatus": document.status.value, "inspection": status, "ageDays": age})
    return findings


def follow_up(backends: Backends, document_ids: Sequence[str]) -> list[str]:
    """The exact commands that finish the repair (the Knowledge Base still lists the deleted documents until a sync)."""

    kb, ds = backends.knowledge_base_id, backends.data_source_id
    commands = [f"aws bedrock-agent start-ingestion-job --knowledge-base-id {kb} --data-source-id {ds}"]
    commands += [
        f'aws bedrock-agent list-knowledge-base-documents --knowledge-base-id {kb} --data-source-id {ds} --max-results 100 '
        f'--query "documentDetails[?contains(identifier.s3.uri, \'{document_id}\')].[status]" --output text'
        for document_id in document_ids
    ]
    return commands


def run_delete(backends: Backends, scope: OperatorScope, document_ids: Sequence[str], *, execute: bool) -> int:
    """Returns the exit code: 0 when every id is done or would be, 1 when something failed or was refused."""

    code = 0
    done: list[str] = []
    for document_id in document_ids:
        status = inspect_document(backends, scope, document_id)
        if status in {"refused_object_present", "binding_invalid", "check_failed"}:
            _emit(documentId=document_id, inspection=status, outcome=status, executed=False)
            code = 1
            continue
        if status == "already_absent":
            _emit(documentId=document_id, inspection=status, outcome="already_absent", executed=False)
            done.append(document_id)  # a second run: the KB follow-up may still be pending
            continue
        if not execute:
            _emit(documentId=document_id, inspection=status, outcome="would_delete", executed=False)
            continue
        report = backends.service.delete_document(scope, document_id)
        failures = sorted({failure.target for failure in report.failures if failure.target in FAILURE_TARGETS} | {
            "other" for failure in report.failures if failure.target not in FAILURE_TARGETS
        })
        if report.ok:
            _emit(documentId=document_id, inspection=status, outcome="deleted", executed=True, indexCleanupPending=report.index_cleanup_pending)
            done.append(document_id)
        else:
            _emit(documentId=document_id, inspection=status, outcome="failed", executed=True, failures=failures)
            code = 1
    if execute and done:
        _emit(next="after the deletes, run these two commands; the second must print nothing once the sync has finished", commands=follow_up(backends, done))
    return code


def build_real_backends() -> Backends:
    """The application's own pieces (verifier, authorization store, repositories, storage). Imported only on the real path."""

    from legaldesk.application import AWSResourceConfig, build_aws_composition  # noqa: PLC0415

    config = AWSResourceConfig.from_environment()
    composition = build_aws_composition(allow_aws=True, config=config)
    return Backends(
        verify_token=lambda token: composition.identity_verifier.verify_authorization_header(f"Bearer {token}"),
        authorization_store=composition.authorization_store,
        metadata_repository=composition.metadata_repository,
        object_storage=composition.object_storage,
        service=OperatorDataGovernanceService(
            metadata_repository=composition.metadata_repository,
            object_storage=composition.object_storage,
            review_repository=composition.review_repository,
        ),
        knowledge_base_id=config.knowledge_base_id,
        data_source_id=config.data_source_id,
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "delete"):
        command = sub.add_parser(name)
        command.add_argument("--tenant", required=True, help="compared with the tenant of the authorized scope; never an authority")
        command.add_argument("--matter", required=True)
    delete = sub.choices["delete"]
    delete.add_argument("--document-id", action="append", required=True, dest="document_ids")
    delete.add_argument("--execute", action="store_true", help="really delete; without it nothing is changed")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, backends: Backends | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _arguments(argv)
    env = os.environ if environ is None else environ
    for value in (args.tenant, args.matter):
        if _IDENTIFIER.fullmatch(value) is None:
            raise SystemExit("--tenant and --matter must be plain identifiers")
    document_ids: list[str] = []
    if args.command == "delete":
        if len(args.document_ids) > MAX_IDS or len(set(args.document_ids)) != len(args.document_ids):
            raise SystemExit(f"at most {MAX_IDS} distinct --document-id values")
        for value in args.document_ids:
            try:
                document_ids.append(str(UUID(value)))
            except ValueError:
                raise SystemExit("every --document-id must be a UUID") from None
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        raise SystemExit(f"{TOKEN_ENV} is required (the access token of a user with an operator role; never an argument)")
    backends = backends or build_real_backends()
    try:
        identity = backends.verify_token(token)
        scope = authorize_operator_scope(identity, args.matter, backends.authorization_store)
    except Exception as exc:  # noqa: BLE001 - one closed answer for every refusal, never the provider's message
        refused = isinstance(exc, (GovernanceAuthorizationError, AuthorizationDenied))
        _emit(outcome="operator_scope_denied", reason="not_an_operator_of_this_matter" if refused else "token_not_verified",
              needs="a verified user with a role in operator/data_steward/admin, the tenant of the matter, and membership of the matter")
        return 2
    if scope.context.tenant_id != args.tenant:
        _emit(outcome="tenant_mismatch")
        return 2
    _emit(scope="authorized", command=args.command, executes=bool(getattr(args, "execute", False)))
    if args.command == "audit":
        findings = audit(backends, scope)
        for finding in findings:
            _emit(**finding)
        _emit(summary="audit", ghosts=len(findings))
        return 0
    return run_delete(backends, scope, document_ids, execute=bool(args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
