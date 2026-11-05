"""Independent source/scorer mutations and the real adapter over fake cloud APIs."""
import contextlib
import copy
import dataclasses
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals import phase15_direction_eval as ev
from evals import phase15_golden_async as asy
from legaldesk.agent_chat import InputDecision
from legaldesk.evidence import ConverseAnswerWriter, ConverseEvidenceResolver
from legaldesk.guardrails import GuardrailGroundingValidator
from test_phase13_grounding import FakeGuardrail, contextual_filters

DATA, SOURCES = ev.load()
CHECKS = ev.verify(DATA, SOURCES)


def directional_case(sentence='The supplier must send written notice to the buyer.'):
    """Scorer-only synthetic grammar challenge, never claimed as uploaded corpus."""
    return {'id': 'oracle-only', 'split': 'test', 'language': 'en',
            'source': {'documentId': 'doc-oracle-only', 'page': 2, 'sentence': sentence},
            'expected': {'kind': 'fact', 'claims': ev.claims(sentence), 'facts': [], 'forbiddenFacts': []}}


def challenge_scorer(scorer):
    case = directional_case()
    verification = {'valid': True}
    def observe(text, page=2):
        return {'text': text, 'citations': [('doc-oracle-only', page)]}
    correct = scorer(case, observe(case['source']['sentence']), verification)
    reverse = scorer(case, observe('The buyer must send written notice to the supplier.'), verification)
    invented = scorer(case, observe('The supplier must pay rent to the buyer.'), verification)
    wrong_page = scorer(case, observe(case['source']['sentence'], 1), verification)
    return [correct['outcome'] == 'correct', reverse['outcome'] == 'fail',
            invented['outcome'] == 'fail', wrong_page['firstFailure'] == 'citations']


def fake_client(case, world):
    """Production Converse/Guardrail adapters, KB edge fake, real worker/interceptor.

    No boto3 factory, no sockets, no external model or Harness invocation.
    """
    golden = ev.golden
    adapted = ev.golden_case(case)
    base = golden.make_pipeline(adapted, world, frozenset())
    absent = case['expected']['kind'] == 'abstention'
    class ConverseCloud:
        def converse(self, **kwargs):
            payload = json.loads(kwargs['messages'][0]['content'][0]['text'])
            if payload.get('mode') == 'separated_answer_writer':
                output = {'answer': case['source']['sentence']}
            else:
                output = {'coverage': 'none' if absent else 'complete', 'conflict': False,
                          'supportingCitationIds': [] if absent else [payload['authorizedPassages'][0]['citationId']]}
            return {'output': {'message': {'role': 'assistant', 'content': [{'text': json.dumps(output)}]}}, 'stopReason': 'end_turn'}
    cloud = ConverseCloud()
    class IndexedCloudKB:
        def retrieve(self, **kwargs):
            ref = case['source']
            doc = world[ref['documentId']]
            # The provider cannot return text from unindexed scanned pages.
            good = golden.GoldenKB._result(golden.TENANT, golden.MATTER, doc, ref['page'], doc.pages[ref['page'] - 1])
            foreign = next(d for d in world.values() if d.matter_id != golden.MATTER)
            bad = golden.GoldenKB._result('tnt_borealis', 'mat_glacier', foreign, 1, foreign.pages[0])
            return {'retrievalResults': [good, bad]}
    ground_cloud = FakeGuardrail({'action': 'NONE', 'outputs': [], 'assessments': [contextual_filters()]})
    service = dataclasses.replace(base.service,
        retrieval_client=IndexedCloudKB(),
        evidence_resolver=ConverseEvidenceResolver(cloud, model_id='FAKE-NO-CLOUD'),
        answer_writer=ConverseAnswerWriter(cloud, model_id='FAKE-NO-CLOUD'),
        grounding_validator=GuardrailGroundingValidator(ground_cloud, base.service.guardrail_config))
    pipeline = golden.make_real_pipeline(adapted, service)
    holder = {}
    def invoke(binding, question, history):
        sim = holder.get('sim')
        if sim is None:
            fake_world = golden.GoldenWorld(world, pipeline, frozenset())
            sim = golden.SimulatedAgent(fake_world, {'simulation': golden.oracle_script(adapted, frozenset())},
                                        harness.grants, harness.auth, golden.RoutingObservation())
            holder['sim'] = sim
        return sim(binding, question, history)
    harness = asy.AsyncHarness(invoke, rag_chat=pipeline.rag_chat)
    pipeline.conversations = harness.conversations
    harness.worker.identity_verifier = SimpleNamespace(verify_authorization_header=lambda _: harness.identity)
    client = golden.DeployedGoldenClient(world=world, worker=harness.worker, service=service,
        input_check=lambda question, correlation: InputDecision('allowed', question),
        access_token=asy._unsigned_token(golden.SUBJECT), matter_id=golden.MATTER,
        plan={'inputChecks': 1, 'harnessInvocations': 1, 'probeRuns': 1, 'asyncJobs': 0, 'probes': 1},
        pipeline_factory=lambda c, conversations: pipeline)
    return client


def dry_real_report():
    world = ev.world_from_sources(SOURCES)
    cases = {c['id']: c for c in DATA['cases']}
    class PerCaseCloud:
        def run(self, adapted):
            return fake_client(cases[adapted['id']], world).run(adapted)
    return ev.run_with_client(DATA, SOURCES, PerCaseCloud(), CHECKS)


class DirectionSourceTests(unittest.TestCase):
    def test_sources_and_status_are_verified_not_fixture_annotations(self):
        self.assertEqual(len(DATA['cases']), 40)
        self.assertEqual(sum(c['canRunNow'] for c in CHECKS['cases']), 40)
        self.assertEqual(sum(d['kbStatus'] == d['metadataStatus'] == 'INDEXED' for d in SOURCES['documents']), 19)
        self.assertEqual(sum(d['kbStatus'] == 'ABSENT' and d['metadataStatus'] == 'FAILED' for d in SOURCES['documents']), 7)
        self.assertTrue(all(c['needsHumanReview'] for c in CHECKS['cases']))

    def test_wrong_sentence_fact_forbidden_fact_and_direction_are_dataset_wrong(self):
        for mutation in ('sentence', 'fact', 'forbidden', 'direction'):
            data = copy.deepcopy(DATA)
            case = data['cases'][0]
            if mutation == 'sentence': case['source']['sentence'] += ' INVENTED'
            if mutation == 'fact': case['expected']['facts'] += ['invented fact']
            if mutation == 'forbidden': case['expected']['forbiddenFacts'] += ['Valencia']
            if mutation == 'direction': case['expected']['claims'] = ev.claims('The landlord must pay rent to the tenant.')
            result = ev.verify(data, SOURCES)['cases'][0]
            self.assertEqual(result['error'], 'dataset_wrong', mutation)
            row = ev.score(case, {'text': case['source']['sentence']}, result)
            self.assertEqual((row['outcome'], row['firstFailure']), ('dataset_wrong', 'dataset'))

    def test_indexing_drift_and_scanned_page_are_not_runnable(self):
        sources = copy.deepcopy(SOURCES)
        doc, _ = ev.source_page(DATA['cases'][0], sources)
        doc['kbStatus'] = 'ABSENT'
        self.assertEqual(ev.verify(DATA, sources)['cases'][0]['error'], 'dataset_wrong')
        data = copy.deepcopy(DATA)
        data['cases'][0]['source'] = {'documentId': 'doc-ext-lease-01-es-long', 'page': 5, 'sentence': 'invented scanned clause'}
        self.assertFalse(ev.verify(data, SOURCES)['cases'][0]['canRunNow'])

    def test_actual_pdf_and_journal_mismatch_are_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            doc = next(d for d in SOURCES['documents'] if d['documentId'] == DATA['cases'][0]['source']['documentId'])
            Path(folder, doc['filename']).write_bytes(b'not original bytes')
            verified = ev.verify(DATA, SOURCES, Path(folder))
            self.assertFalse(verified['cases'][0]['valid'])
            journal = Path(folder, 'journal.jsonl')
            journal.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'journal changed'):
                ev.verify(DATA, SOURCES, journal=journal)

    def test_mechanical_direction_and_order_are_independently_derived(self):
        c = directional_case()
        self.assertEqual(ev.derive_source(c, c['source']['sentence']), 'grammar')
        c['expected']['claims'][0]['actor'] = 'buyer'
        with self.assertRaises(ValueError): ev.derive_source(c, c['source']['sentence'])

    def test_review_sheet_has_exactly_ten_random_unchecked_samples(self):
        sheet = ev.review_sheet(DATA, CHECKS)
        self.assertEqual(sheet.count('SAMPLE — unchecked'), 10)
        self.assertEqual(sum('| dir-' in line for line in sheet.splitlines()), 40)
        self.assertIn('A human must check', sheet)

    def test_golden_hash_and_gate_are_unchanged(self):
        _, digest = ev.golden.load_dataset()
        self.assertEqual(digest, ev.golden.EXPECTED_DATASET_SHA256)
        self.assertIsNone(ev.report(DATA, SOURCES, [], 'offline')['qualityGate'])

    def test_journal_confirmation_is_not_indexing_and_absent_kb_is_preserved(self):
        from scripts.phase15_direction_status import reconcile
        rows = reconcile([{'state': 'confirmed', 'goldenId': 'doc-a', 'documentId': 'real-a', 'sha256': 'h'}],
                         [{'documentId': 'real-a', 'status': 'FAILED', 's3Key': 'prefix/a.pdf'}], [])
        self.assertEqual((rows[0]['metadataStatus'], rows[0]['kbStatus']), ('FAILED', 'ABSENT'))

    def test_live_status_reader_paginates_both_services_and_uses_only_reads(self):
        from scripts.phase15_direction_status import collect
        calls = []
        class Table:
            def query(self, **kwargs):
                calls.append(('query', kwargs))
                return {'Items': [{'documentId': 'b'}]} if 'ExclusiveStartKey' in kwargs else {'Items': [{'documentId': 'a'}], 'LastEvaluatedKey': {'pk': 'next'}}
        class KB:
            def list_knowledge_base_documents(self, **kwargs):
                calls.append(('list', kwargs))
                return {'documentDetails': [{'status': 'INDEXED'}]} if 'nextToken' in kwargs else {'documentDetails': [], 'nextToken': 'next'}
        class Session:
            def client(self, name, **kwargs):
                self_config = kwargs['config']
                self_attempts = self_config.retries['total_max_attempts']
                assert self_attempts == 1
                return SimpleNamespace(get_caller_identity=lambda: {'Account': '344774635844'}) if name == 'sts' else KB()
            def resource(self, *args, **kwargs):
                return SimpleNamespace(Table=lambda _: Table())
        metadata, documents = collect(Session())
        self.assertEqual((len(metadata), len(documents)), (2, 1))
        self.assertEqual([name for name, _ in calls], ['query', 'query', 'list', 'list'])
        self.assertTrue(calls[0][1]['ConsistentRead'])


class DirectionOracleMutationTests(unittest.TestCase):
    def test_correct_reversed_invented_and_wrong_page(self):
        self.assertTrue(all(challenge_scorer(ev.score)))

    def test_always_passing_scorer_fails_the_challenge(self):
        self.assertFalse(all(challenge_scorer(lambda *args: {'outcome': 'correct', 'firstFailure': None})))

    def test_always_correct_oracle_and_missing_direction_parser_fail_challenge(self):
        with patch.object(ev, 'oracle', return_value=('correct', 'mutant')):
            self.assertFalse(all(challenge_scorer(ev.score)))
        with patch.object(ev, 'claims', return_value=[]):
            self.assertFalse(all(challenge_scorer(ev.score)))

    def test_lexical_overlap_cannot_accept_reversal(self):
        c = directional_case()
        reverse = 'The buyer must send written notice to the supplier.'
        self.assertEqual(set(ev.golden.norm(reverse).split()), set(ev.golden.norm(c['source']['sentence']).split()))
        self.assertEqual(ev.oracle(c, reverse)[0], 'wrong')

    def test_four_evasions_are_rejected_and_correct_paraphrases_are_accepted(self):
        for source, correct, reverse in [
            ('The supplier must send written notice to the buyer.', 'The supplier has a duty to remit written notice to the buyer.', 'The buyer has a duty to remit written notice to the supplier.'),
            ('The supplier must send written notice to the buyer.', 'The supplier must dispatch written notice to the buyer.', 'The buyer must dispatch written notice to the supplier.'),
            ('El proveedor debe notificar al comprador.', 'Incumbe al proveedor notificar al comprador.', 'Incumbe al comprador notificar al proveedor.'),
            ('El proveedor debe notificar al comprador.', 'El proveedor debe cursar un aviso al comprador.', 'El comprador debe cursar un aviso al proveedor.')]:
            c = directional_case(source)
            self.assertEqual(ev.oracle(c, correct)[0], 'correct', correct)
            self.assertEqual(ev.oracle(c, reverse)[0], 'wrong', reverse)

    def test_object_negation_and_condition_cannot_be_dropped(self):
        original = 'The supplier must not send written notice to the buyer unless the buyer defaults.'
        c = directional_case(original)
        for mutated in [original.replace('not ', ''), original.replace(' unless the buyer defaults', ''),
                        original.replace('written notice', 'secret credentials'), original.replace('buyer defaults', 'supplier defaults')]:
            self.assertEqual(ev.oracle(c, mutated)[0], 'wrong', mutated)
        self.assertEqual(ev.oracle(c, original)[0], 'correct')

    def test_opposite_directions_and_similar_roles_are_not_bag_of_names(self):
        source = 'The buyer one must pay fees to the buyer two. The buyer two must deliver goods to the buyer one.'
        c = directional_case(source)
        self.assertEqual(len(c['expected']['claims']), 2)
        self.assertEqual(ev.oracle(c, source)[0], 'correct')
        self.assertEqual(ev.oracle(c, source.replace('pay fees', 'deliver goods'))[0], 'wrong')

    def test_past_fact_does_not_become_obligation(self):
        c = directional_case('The supplier delivered goods to the buyer.')
        self.assertEqual(ev.claims(c['source']['sentence']), [])
        self.assertEqual(ev.oracle(c, c['source']['sentence'])[0], 'correct')
        self.assertEqual(ev.oracle(c, 'The supplier must deliver goods to the buyer.')[0], 'wrong')

    def test_abstention_unrelated_fact_and_unknown_prose(self):
        self.assertEqual(ev.oracle(DATA['cases'][0], DATA['cases'][0]['source']['sentence'])[0], 'correct')
        self.assertEqual(ev.oracle(DATA['cases'][0], '', abstained=True)[1], 'over_abstention')
        self.assertEqual(ev.oracle(DATA['cases'][2], '', abstained=True)[1], 'correct_abstention')
        self.assertEqual(ev.oracle(directional_case(), 'Payment is incumbent upon the recipient.')[0], 'review')

    def test_every_attribution_stage_is_observable_and_first_failure_wins(self):
        c = directional_case()
        for phase in ('retrieval', 'resolver', 'writer', 'detector', 'grounding'):
            obs = {'text': 'The buyer must send written notice to the supplier.', phase: 'fail'}
            row = ev.score(c, obs, {'valid': True})
            self.assertEqual(row['firstFailure'], phase)
            self.assertIn('answer', row['alsoFailed'])
        row = ev.score(c, {'text': '', 'writer': 'fail', 'grounding': 'fail'}, {'valid': False})
        self.assertEqual(row['firstFailure'], 'dataset')

    def test_report_does_not_contain_text_and_is_create_only(self):
        c = DATA['cases'][0]
        row = ev.score(c, {'text': c['source']['sentence']}, CHECKS['cases'][0])
        report = ev.report(DATA, SOURCES, [row], 'offline')
        serialized = json.dumps(report)
        self.assertNotIn(c['question'], serialized)
        self.assertNotIn(c['source']['sentence'], serialized)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'report.json')
            ev.write_new(path, report)
            with self.assertRaises(FileExistsError): ev.write_new(path, report)

    def test_cost_arithmetic_uses_run_d_and_counts_all_probe_calls(self):
        plan = ev.cost_plan(DATA, 40)
        self.assertEqual(plan['typicalModelCalls'], 208)
        self.assertEqual(plan['upperModelCalls'], 1520)
        self.assertEqual(plan['harnessInvocations'], 40)
        self.assertFalse(plan['upperIsBillingCap'])
        self.assertGreater(plan['upperPlanningUsd'], plan['typicalPlanningUsd'])


class RealAdapterFakeCloudTests(unittest.TestCase):
    def test_all_40_cases_cross_real_adapter_and_no_network(self):
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            with contextlib.redirect_stdout(io.StringIO()):
                report = dry_real_report()
        self.assertEqual(len(report['rows']), 40)
        self.assertEqual(report['counts']['error'], 0)
        self.assertTrue(all(r['stages']['dataset'] == 'pass' for r in report['rows']))
        self.assertTrue(all(r['stages']['retrieval'] == 'pass' for r in report['rows']))
        self.assertTrue(all(r['stages']['resolver'] == 'pass' for r in report['rows']))
        # Source-backed correct sentences may be blocked by current production
        # behavior; this is observable evidence, not an annotation to waive.
        self.assertTrue(all(r['outcome'] in ('correct', 'fail', 'human_review') for r in report['rows']))


if __name__ == '__main__':
    unittest.main()
