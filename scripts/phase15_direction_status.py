"""Read-only reconciliation: upload journal -> DynamoDB -> Bedrock document list.

No retrieval, inference, ingestion, upload or AWS mutation API is used.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.phase15_direction_eval import SOURCES, write_new


def reconcile(journal, metadata, kb_documents):
    confirmed = {r['goldenId']: r for r in journal if r['state'] == 'confirmed'}
    by_id = {r['documentId']: r for r in metadata}
    result = []
    for gid, record in confirmed.items():
        doc = by_id.get(record['documentId'], {})
        match = [r for r in kb_documents if r['identifier']['s3']['uri'].endswith('/' + doc.get('s3Key', 'NO_MATCH'))]
        # More than one match is a data-quality problem, never choose arbitrarily.
        if len(match) > 1:
            raise ValueError('ambiguous KB document')
        result.append({'documentId': gid, 'pdfSha256': record['sha256'],
                       'metadataStatus': doc.get('status', 'ABSENT'),
                       'kbStatus': match[0]['status'] if match else 'ABSENT'})
    return result


def collect(session):
    from botocore.config import Config
    from boto3.dynamodb.conditions import Key
    config = Config(retries={'total_max_attempts': 1}, read_timeout=20, connect_timeout=10)
    sts = session.client('sts', config=config)
    if sts.get_caller_identity()['Account'] != '344774635844':
        raise ValueError('unexpected AWS account')
    table = session.resource('dynamodb', config=config).Table('LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T')
    kwargs = {'KeyConditionExpression': Key('pk').eq('TENANT#tnt_phase14_20260928#MATTER#mat_phase14_b_20260928') & Key('sk').begins_with('DOCUMENT#'), 'ConsistentRead': True}
    metadata = []
    while True:
        result = table.query(**kwargs)
        metadata.extend(result['Items'])
        if not result.get('LastEvaluatedKey'):
            break
        kwargs['ExclusiveStartKey'] = result['LastEvaluatedKey']
    client = session.client('bedrock-agent', config=config)
    kwargs = {'knowledgeBaseId': 'AQWCFL7239', 'dataSourceId': 'XIA81RBIPG', 'maxResults': 100}
    documents = []
    while True:
        result = client.list_knowledge_base_documents(**kwargs)
        documents.extend(result['documentDetails'])
        if not result.get('nextToken'):
            break
        kwargs['nextToken'] = result['nextToken']
    return metadata, documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profile', default='legaldesk')
    args = parser.parse_args()
    import boto3
    metadata, documents = collect(boto3.Session(profile_name=args.profile, region_name='eu-west-1'))
    raw = args.journal.read_bytes()
    rows = reconcile([json.loads(line) for line in raw.decode().splitlines()], metadata, documents)
    expected = json.loads(SOURCES.read_text(encoding='utf-8'))
    by_id = {d['documentId']: d for d in expected['documents']}
    drift = [r['documentId'] for r in rows if any(r[k] != by_id[r['documentId']][k] for k in ('pdfSha256', 'metadataStatus', 'kbStatus'))]
    result = {'schemaVersion': 'phase15-direction-index-status-1', 'checkedAt': datetime.now(timezone.utc).isoformat(),
              'journalSha256': hashlib.sha256(raw).hexdigest(), 'readOnly': True, 'paginationComplete': True,
              'rows': rows, 'drift': drift}
    write_new(args.output, result)
    print(json.dumps({'documents': len(rows), 'indexed': sum(r['kbStatus'] == 'INDEXED' for r in rows), 'drift': drift}))
    return int(bool(drift) or len(rows) != 26)


if __name__ == '__main__':
    raise SystemExit(main())
