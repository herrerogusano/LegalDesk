"""Exploratory direction measurement. No model/network entry point and no quality gate.

The bounded oracle is independent of the production detector. Unknown linguistic
forms are REVIEW, never silently correct. Offline results prove plumbing only.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'tests', ROOT / 'backend/src', ROOT / 'agent/src'):
    sys.path.insert(0, str(path))
from evals import phase15_golden_eval as golden

DATASET = ROOT / 'evals/phase15_direction_dataset.json'
SOURCES = ROOT / 'evals/phase15_direction_sources.json'
PIN = ROOT / 'evals/phase15_direction_dataset.sha256'
PHASES = ('dataset', 'retrieval', 'resolver', 'writer', 'detector', 'grounding', 'citations', 'answer')
PROPOSED_THRESHOLDS = {'status': 'proposal_only', 'reversalsMax': 0, 'inventedObligationsMax': 0,
                       'wrongPageMax': 0, 'overAbstentionRateMax': .10, 'humanReviewedAccuracyMin': .90}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def load():
    raw = DATASET.read_bytes()
    if sha(raw) != PIN.read_text().strip():
        raise ValueError('direction dataset hash mismatch')
    data, sources = json.loads(raw), json.loads(SOURCES.read_text(encoding='utf-8'))
    if data['sourcesSha256'] != sha(SOURCES.read_bytes()):
        raise ValueError('source snapshot hash mismatch')
    if len({c['id'] for c in data['cases']}) != len(data['cases']):
        raise ValueError('duplicate case')
    return data, sources


def source_page(case, sources):
    ref = case['source']
    doc = next(d for d in sources['documents'] if d['documentId'] == ref['documentId'])
    return doc, doc['pages'][ref['page'] - 1]


# This grammar deliberately does not import production token vocabularies.
# Sentence-local actor + modal + action + recipient parsing, including four
# production-detector evasions. Conditions and negation are semantic fields.
ACTION = r'pay|notify|send|deliver|return|remit|dispatch|notificar|pagar|abonar|devolver|entregar|cursar un aviso'
MODAL = r'must|shall|has a duty to|has to|is obliged to|debe|deberá|tiene que|ha de'
CLAIM = re.compile(
    rf'(?P<actor>(?:the |el |la )?[\wÁ-ÿ][\wÁ-ÿ -]*?)\s+'
    rf'(?P<modal>{MODAL})\s+(?P<neg>not |no )?(?P<action>{ACTION})\b'
    r'(?P<object>.*?)\s+(?:to|al?|a la)\s+(?P<recipient>[\wÁ-ÿ][\wÁ-ÿ -]*?)'
    r'(?P<condition>\s+(?:unless|if|salvo que|si)\s+[^.!?]+)?(?=[.!?]|$)', re.I)
INCUMBE = re.compile(r'Incumbe al (?P<actor>[\wÁ-ÿ -]+?) (?P<action>notificar) al '
                     r'(?P<recipient>[\wÁ-ÿ -]+?)(?=[.!?]|$)', re.I)
FAMILIES = {'remit': 'send', 'dispatch': 'send', 'cursar un aviso': 'notify', 'notificar': 'notify',
            'pagar': 'pay', 'abonar': 'pay', 'devolver': 'return', 'entregar': 'deliver'}


def entity(text):
    return re.sub(r'^(the|el|la|al|a la) ', '', golden.norm(text))


def claims(text):
    """Return independent directed claims; unresolved syntax is handled by oracle()."""
    result = []
    for match in list(CLAIM.finditer(text)) + list(INCUMBE.finditer(text)):
        d = match.groupdict()
        result.append({'actor': entity(d['actor']), 'action': FAMILIES.get(d['action'].lower(), d['action'].lower()),
                       'object': golden.norm(d.get('object') or ''),
                       'recipient': entity(d['recipient']), 'negated': bool(d.get('neg')),
                       'condition': golden.norm(d.get('condition') or '')})
    return result


def derive_source(case, page):
    sentence = case['source']['sentence']
    if sentence not in page:
        raise ValueError('source sentence absent')
    derived = claims(sentence)
    declared = case['expected']['claims']
    if declared != derived:
        raise ValueError('declared direction disagrees with independent grammar')
    # Exact ordered spans, with no crossing of sentences. Direction cannot be
    # established just because the same party names occur somewhere on a page.
    for match in list(CLAIM.finditer(sentence)) + list(INCUMBE.finditer(sentence)):
        if not (match.start('actor') < match.start('action') < match.start('recipient')):
            raise ValueError('actor/action/recipient order unsupported')
    return 'grammar' if derived else 'human_review_no_complete_triple'


def verify(data, sources, corpus_dir=None, journal=None):
    """Verify PDF bytes and fresh pypdf extraction when actual files are supplied.

    A snapshot-only CI check is explicitly weaker than actual-file verification.
    The upload journal is provisioning evidence, NOT an indexing-status oracle.
    """
    from pypdf import PdfReader
    journal_rows = None
    if journal:
        raw = Path(journal).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sources['journalSha256']:
            raise ValueError('journal changed: refresh indexing evidence before running')
        journal_rows = {r['goldenId']: r for r in map(json.loads, raw.decode().splitlines()) if r['state'] == 'confirmed'}
    extracted, document_errors = {}, {}
    for doc in sources['documents']:
        if not corpus_dir:
            continue
        path = (ROOT / 'tests/fixtures/idp/pdfs' if doc['location'] == 'fixture' else Path(corpus_dir)) / doc['filename']
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != doc['pdfSha256']:
                raise ValueError('pdf hash mismatch')
            if journal_rows is not None and journal_rows[doc['documentId']]['sha256'] != doc['pdfSha256']:
                raise ValueError('journal PDF hash mismatch')
            try:
                pages = [p.extract_text() or '' for p in PdfReader(path).pages]
            except Exception:
                if doc['metadataStatus'] != 'FAILED' or doc['pages']:
                    raise
                pages = []  # intentionally broken failed-upload PDF
            if pages != doc['pages']:
                raise ValueError('actual extracted text differs from pinned snapshot')
            extracted[doc['documentId']] = pages
        except (OSError, ValueError, KeyError) as exc:
            document_errors[doc['documentId']] = type(exc).__name__
    rows = []
    for case in data['cases']:
        method, error, runnable = 'unverified', None, False
        try:
            doc, page = source_page(case, sources)
            if doc['documentId'] in document_errors:
                raise ValueError('actual source invalid')
            method = derive_source(case, page)
            sentence = case['source']['sentence']
            for fact in case['expected']['facts']:
                if fact not in sentence:
                    raise ValueError('required fact absent from exact sentence')
            for fact in case['expected']['forbiddenFacts']:
                if fact in sentence:
                    raise ValueError('forbidden fact actually supported')
            runnable = doc['metadataStatus'] == doc['kbStatus'] == 'INDEXED' and bool(page.strip())
            if case['canRunNow'] != runnable:
                raise ValueError('runnability annotation stale')
        except (ValueError, KeyError, IndexError, StopIteration):
            error = 'dataset_wrong'
        rows.append({'id': case['id'], 'valid': error is None, 'canRunNow': runnable and error is None,
                     'directionVerification': method, 'needsHumanReview': method != 'grammar', 'error': error})
    return {'scope': 'actual_pdf_files' if corpus_dir else 'pinned_extraction_snapshot_only',
            'actualDocumentsChecked': len(extracted), 'documentErrors': document_errors, 'cases': rows}


def oracle(case, text, *, abstained=False):
    """Three-valued verdict: correct / wrong / review, never lexical overlap alone.

    Exact sentence responses are supported within this page scope. For arbitrary
    prose, every directed assertion must parse and match actor/action/recipient,
    negation and condition. Unknown paraphrases require human adjudication.
    """
    expected = case['expected']
    found = claims(text)
    if found != expected['claims'] and found:
        reversed_direction = any(
            a['actor'] == b['recipient'] and a['recipient'] == b['actor']
            and all(a[k] == b[k] for k in ('action', 'object', 'negated', 'condition'))
            for a in found for b in expected['claims'])
        return 'wrong', 'reversed_direction' if reversed_direction else 'invented_or_changed_obligation'
    if expected['kind'] == 'abstention':
        if abstained and not found:
            return 'correct', 'correct_abstention'
        return ('wrong', 'invented_obligation') if found else ('review', 'abstention_semantics_unverified')
    if abstained:
        return 'wrong', 'over_abstention'
    # The source sentence is a safe, independently anchored correct answer.
    if ' '.join(text.split()) == ' '.join(case['source']['sentence'].split()):
        return 'correct', 'exact_source_answer'
    if any(golden.contains(golden.norm(text), f) for f in expected['forbiddenFacts']):
        return 'wrong', 'forbidden_fact'
    if not all(golden.contains(golden.norm(text), f) for f in expected['facts']):
        return 'review', 'unrecognized_answer'
    if found and found == expected['claims']:
        return 'correct', 'parsed_direction'
    return 'review', 'free_prose_requires_human_review'


def score(case, observation, verification):
    """First observed failure, retaining independent probe and final verdicts.

    Missing telemetry stays unobserved; detector eligibility is not inferred
    from a writer error. Writer text and output are used in memory only.
    """
    stages = {p: 'unobserved' for p in PHASES}
    stages['dataset'] = 'pass' if verification['valid'] else 'fail'
    text = observation.get('text', '')
    verdict, reason = oracle(case, text, abstained=observation.get('abstained', False))
    for phase in ('retrieval', 'resolver', 'writer', 'detector', 'grounding'):
        if observation.get(phase) in ('pass', 'fail', 'na'):
            stages[phase] = observation[phase]
    gold = (case['source']['documentId'], case['source']['page'])
    if observation.get('citations') is not None:
        pairs = {tuple(x) for x in observation['citations']}
        stages['citations'] = 'pass' if (not pairs if observation.get('abstained') else pairs == {gold}) else 'fail'
    if observation.get('writerText') is not None:
        writer_verdict, _ = oracle(case, observation['writerText'])
        if writer_verdict == 'wrong':
            stages['writer'] = 'fail'
    stages['answer'] = {'correct': 'pass', 'wrong': 'fail', 'review': 'unobserved'}[verdict]
    failures = [p for p in PHASES if stages[p] == 'fail']
    return {'id': case['id'], 'split': case['split'], 'language': case['language'], 'stages': stages,
            'outcome': 'dataset_wrong' if stages['dataset'] == 'fail' else 'fail' if failures else 'human_review' if verdict == 'review' else 'correct',
            'firstFailure': failures[0] if failures else None, 'alsoFailed': failures[1:],
            'finalVerdict': verdict, 'reason': reason, 'answerSha256': sha(text.encode()),
            'detectorObserved': stages['detector'] != 'unobserved'}


def golden_case(case):
    """Thin adapter; golden dataset, thresholds, evaluator and gates stay untouched."""
    absent = case['expected']['kind'] == 'abstention'
    return {'id': case['id'], 'question': case['question'], 'category': 'legal_text', 'difficulty': 'hard',
            'split': case['split'], 'documents': [case['source']['documentId']],
            'expected': {'input': 'allowed', 'route': {'kind': 'grounded'}, 'path': 'grounded',
                         'rag': {'status': 'insufficient_evidence' if absent else 'answerable',
                                 'gold': [[case['source']['documentId'], case['source']['page']]]},
                         'facts': {'mustContain': [[f] for f in case['expected']['facts']], 'mustNotContain': case['expected']['forbiddenFacts']}}}


def world_from_sources(sources):
    data, _ = golden.load_dataset()
    world = golden.build_world(data)
    for doc in sources['documents']:
        world[doc['documentId']].pages = doc['pages']
        world[doc['documentId']].status = doc['metadataStatus']
    return world


def observation_from_golden(case, obs, capture=None):
    blocks = (obs.output or {}).get('blocks', [])
    text = ' '.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
    pairs = [(c['documentId'], c['pageNumber']) for c in (obs.output or {}).get('citations', [])]
    record = obs.pipeline
    result = {'text': text, 'citations': pairs, 'abstained': (obs.final or {}).get('evidenceStatus') == 'insufficient_evidence'}
    if record:
        gold = (case['source']['documentId'], case['source']['page'])
        result['retrieval'] = 'fail' if 'knowledge_base_retrieve:error' in record.operations else 'pass' if gold in record.retrieved else 'fail'
        if record.resolver_ran:
            result['resolver'] = 'pass' if (not record.supporting if case['expected']['kind'] == 'abstention' else gold in record.supporting) else 'fail'
        result['writer'] = 'fail' if record.writer_error else 'pass' if record.writer_ran else 'na' if case['expected']['kind'] == 'abstention' else 'unobserved'
        result['grounding'] = {'ok': 'pass', 'blocked': 'fail', 'error': 'fail'}.get(record.grounding, 'unobserved')
    if capture:
        result.update(capture)
    return result


def run_with_client(data, sources, client, verification, *, mode='real_adapter_fake_cloud'):
    """Accept the existing DeployedGoldenClient; no cloud factory is called here.

    Its pipeline is a separate sample, exactly as in the golden evaluation.
    The final direction score uses final public text, not the case annotations.
    """
    if mode not in ('real_adapter_fake_cloud', 'real_authorized'):
        raise ValueError('unknown observation mode')
    checks = {r['id']: r for r in verification['cases']}
    rows = []
    for case in data['cases']:
        if not checks[case['id']]['canRunNow']:
            rows.append({'id': case['id'], 'outcome': 'not_run', 'firstFailure': 'dataset'})
            continue
        obs = client.run(golden_case(case))
        row = score(case, observation_from_golden(case, obs), checks[case['id']])
        if obs.error:
            row.update(outcome='error', error='adapter_error')
        row['probeIsSeparateSample'] = True
        rows.append(row)
    return report(data, sources, rows, mode)


def report(data, sources, rows, mode):
    return {'schemaVersion': 'phase15-direction-report-1', 'mode': mode, 'qualityGate': None,
            'proves': 'model_sample_needs_human_review' if mode == 'real_authorized' else 'machinery_only_not_model_quality',
            'datasetSha256': sha(DATASET.read_bytes()),
            'sourcesSha256': sha(SOURCES.read_bytes()), 'rows': rows,
            'counts': {v: sum(r['outcome'] == v for r in rows) for v in ('correct', 'fail', 'human_review', 'dataset_wrong', 'error', 'not_run')},
            'firstFailures': {p: sum(r.get('firstFailure') == p for r in rows) for p in PHASES},
            'indexStatusObservedAt': sources['checkedAt'],
            'humanSampleRequired': 10, 'humanSampleCompleted': False,
            'proposedThresholds': PROPOSED_THRESHOLDS}


def review_sheet(data, verification):
    chosen = set(random.Random(1519).sample([c['id'] for c in data['cases']], 10))
    by_id = {r['id']: r for r in verification['cases']}
    lines = ['# Direction review sheet', '',
             'A human must check at least the ten SAMPLE rows before using model-quality results. '
             'No human review has been completed. Absence/implicit direction requires human review too.', '',
             '| Case | Sample | Question | Exact PDF source (line wraps shown as ↵) | Expected direction / result | Verification | Run now |',
             '|---|---|---|---|---|---|---|']
    for c in data['cases']:
        v = by_id[c['id']]
        cells = [c['id'], 'SAMPLE — unchecked' if c['id'] in chosen else '', c['question'],
                 f"{c['source']['documentId']} p{c['source']['page']}: {c['source']['sentence']}",
                 c['expected']['directionExplanation'], v['directionVerification'] + '; ' + verification['scope'],
                 'yes (human review pending)' if v['canRunNow'] else 'no']
        lines.append('| ' + ' | '.join(x.replace('|', '\\|').replace('\n', '↵') for x in cells) + ' |')
    return '\n'.join(lines) + '\n'


def write_new(path, payload):
    with Path(path).open('x', encoding='utf-8', newline='\n') as out:
        out.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def cost_plan(data, count):
    """Run-d empirical anchor plus the existing historical local price model.

    Upper is a planning scenario, NOT a billing cap: 4 Harness turns, 16 tool
    results each treated as a search (2 model calls), and a 2-call probe.
    Managed service internal work is not preventively bounded by this runner.
    """
    from scripts.phase15_golden_cost_model import probe_usd
    typical_probe = sum(probe_usd('insufficient_evidence' if c['expected']['kind'] == 'abstention' else 'answerable', upper=False)
                        for c in data['cases'][:count])
    probe_calls = sum(1 if c['expected']['kind'] == 'abstention' else 2 for c in data['cases'][:count])
    upper_calls = count * (4 + 16 * 2 + 2)
    return {'caseCount': count, 'harnessInvocations': count, 'probeRuns': count,
            'typicalModelCalls': round(count * 111 / 30) + probe_calls,
            'upperModelCalls': upper_calls, 'upperIsBillingCap': False,
            'typicalPlanningUsd': round(count * 1.72 / 30 + typical_probe + .10, 2),
            'upperPlanningUsd': round(upper_calls * 1.72 / 111 + .50, 2),
            'basis': 'run_d_30_cases_111_calls_1.72_usd_plus_historical_probe_model',
            'asyncJobs': 0, 'uploads': 0, 'retries': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('verify', 'offline', 'preflight', 'review-sheet'))
    parser.add_argument('--corpus-dir', type=Path)
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    data, sources = load()
    verification = verify(data, sources, args.corpus_dir, args.journal)
    if args.mode == 'review-sheet':
        if not args.output:
            parser.error('--output required')
        with args.output.open('x', encoding='utf-8', newline='\n') as out:
            out.write(review_sheet(data, verification))
        return 0
    if args.mode == 'verify':
        result = verification
    elif args.mode == 'preflight':
        n = sum(r['canRunNow'] for r in verification['cases'])
        result = {'executionEnabled': False, 'questionBeforeExecution': data['questionBeforeExecution'],
                  **cost_plan(data, n),
                  'qualityGate': None, 'dependency': 'PR116 deployed; owner approval and human review pending'}
    else:
        rows = []
        for c, v in zip(data['cases'], verification['cases']):
            absent = c['expected']['kind'] == 'abstention'
            obs = {'text': '' if absent else c['source']['sentence'], 'abstained': absent,
                   'citations': [] if absent else [(c['source']['documentId'], c['source']['page'])]}
            rows.append(score(c, obs, v))
        result = report(data, sources, rows, 'offline_oracle_dry_run')
    if args.output:
        write_new(args.output, result)
    print(json.dumps(result if args.mode != 'offline' else result['counts'], ensure_ascii=True))
    return int(any(not r['valid'] for r in verification['cases']))


if __name__ == '__main__':
    raise SystemExit(main())
