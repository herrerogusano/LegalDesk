"""Operator-invoked, single Knowledge Base sync for selected uploaded docs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from legaldesk.documents import Boto3DynamoDocumentMetadataRepository, Boto3S3ObjectStorage
from legaldesk.ingestion import DocumentScopeRef, run_knowledge_base_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--data-source-id", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--document-bucket", required=True)
    parser.add_argument(
        "--document",
        action="append",
        required=True,
        metavar="TENANT_ID,MATTER_ID,DOCUMENT_ID",
        help="explicit uploaded document scope; repeat for each selected document",
    )
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--timeout-seconds", type=int, default=1_800)
    args = parser.parse_args()
    document_refs = []
    for value in args.document:
        parts = value.split(",")
        if len(parts) != 3 or any(not part.strip() for part in parts):
            parser.error("each --document must be TENANT_ID,MATTER_ID,DOCUMENT_ID")
        document_refs.append(DocumentScopeRef(*(part.strip() for part in parts)))

    import boto3

    client = boto3.client("bedrock-agent", region_name=args.region)
    s3_client = boto3.client("s3", region_name=args.region)
    repository = Boto3DynamoDocumentMetadataRepository(args.table_name)
    result = run_knowledge_base_sync(
        client=client,
        object_verifier=Boto3S3ObjectStorage(args.document_bucket, client=s3_client),
        metadata_repository=repository,
        knowledge_base_id=args.knowledge_base_id,
        data_source_id=args.data_source_id,
        document_refs=document_refs,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        "Knowledge Base sync "
        f"job={result.ingestion_job_id} status={result.status} "
        f"documents_updated={result.documents_updated} "
        f"failed_document_count={result.failed_document_count}"
    )
    if result.status == "TIMED_OUT":
        return 2
    if result.status == "COMPLETE" and result.failed_document_count != 0:
        print("Selected documents remain PENDING_INGESTION for operator reconciliation.")
        return 3
    return 0 if result.status == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
