"""Author the exploratory set from pinned PDF extraction, never fixture prose.

Run only before freezing a new owner-reviewed dataset version. No cloud calls.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.phase15_direction_eval import claims, sha

# doc suffix, page, exact start/end, kind, required source facts, coverage,
# English question, Spanish question, direction explanation.
SPECS = [
('ext-notice-01-es-short', 1, 'Los bienes', 'en Valencia.', 'fact', ['Puerto Claro', 'Valencia'], 'return_destination',
 'Where must goods be returned?', '¿Dónde deben devolverse los bienes?', 'Puerto Claro warehouse in Valencia; actor unstated.'),
('ext-notice-01-es-short', 1, 'Los bienes', 'en Valencia.', 'abstention', [], 'return_actor_missing',
 'Does this page expressly require Alborada to return goods to Puerto Claro?', '¿Exige expresamente esta página que Alborada devuelva los bienes a Puerto Claro?', 'Passive return: actor unstated; De/Para is not proof of the debtor.'),
('ext-notice-01-es-short', 1, 'Fecha del aviso:', '2026.', 'fact', ['3 de noviembre de 2026'], 'unrelated_obligation_false_rejection',
 'What is the notice date?', '¿Qué fecha tiene el aviso?', 'Date only; unrelated return obligation must not force rejection.'),
('ext-notice-01-es-short', 1, 'Por el presente', 'relación comercial.', 'abstention', [], 'unknown_modal_question',
 'Does Puerto Claro have a duty to remit written notice to Alborada?', '¿Incumbe a Puerto Claro notificar a Alborada?', 'Communication of nonrenewal is not an express reversed notification duty.'),
('ext-lease-01-es-long', 4, 'Quinta.', 'anticipados.', 'abstention', [], 'reversed_premise',
 'Does this page expressly require the landlord to pay the tenant?', '¿Exige expresamente esta página que la arrendadora pague a la arrendataria?', 'Rent amount/timing only: no named debtor or creditor on this page.'),
('ext-lease-01-es-long', 4, 'Quinta.', 'anticipados.', 'abstention', [], 'plausible_premise_unstated',
 'Does this page expressly name the tenant as payer and landlord as recipient?', '¿Nombra expresamente esta página a la arrendataria como pagadora y a la arrendadora como receptora?', 'Do not turn a plausible legal inference into express documentary direction.'),
('ext-msa-01-en-long', 4, 'Undisputed invoices', 'of receipt.', 'abstention', [], 'pay_actor_missing',
 'Who must pay whom under the undisputed-invoice sentence?', '¿Quién debe pagar a quién según la frase de facturas no discutidas?', 'Passive payable wording: timing, not debtor/creditor.'),
('ext-msa-01-en-long', 4, 'Disputed amounts', 'of the invoice.', 'abstention', [], 'notify_actor_missing',
 'Who must notify whom about disputed amounts?', '¿Quién debe cursar un aviso a quién sobre importes discutidos?', 'Passive notification deadline: actor/recipient unstated.'),
('ext-msa-01-en-long', 4, 'Undisputed invoices', 'of receipt.', 'fact', ['forty-five (45) days'], 'pay_timing',
 'Within how many days are undisputed invoices payable?', '¿En cuántos días son pagaderas las facturas no discutidas?', '45 days; no named direction.'),
('ext-msa-01-en-long', 4, 'Disputed amounts', 'of the invoice.', 'fact', ['ten (10) days'], 'notify_timing',
 'What is the notification period for disputed amounts?', '¿Qué plazo hay para notificar los importes discutidos?', '10 days; no inference of sender or recipient.'),
('ext-msa-01-en-long', 3, 'The Supplier provides', 'work order.', 'fact', ['Supplier', 'Services'], 'services_fact',
 'Who provides services described in work orders?', '¿Quién presta los servicios descritos en órdenes de trabajo?', 'Supplier provides services; recipient unstated.'),
('ext-msa-01-en-long', 7, 'Either Party may terminate for convenience', 'written notice.', 'fact', ['ninety (90) days'], 'permission_not_duty',
 'What convenience-termination notice period is permitted?', '¿Qué preaviso permite la terminación por conveniencia?', 'Permission with 90 days notice is not an unconditional duty to notify.'),
('judgment-01-en-digital', 1, 'Costs: Each party', 'its own costs.', 'fact', ['Each party', 'its own costs'], 'true_premise_self_costs',
 'Does each party have to bear its own costs?', '¿Tiene cada parte que soportar sus propias costas?', 'Yes: self-burden, not a transfer to another party.'),
('judgment-01-en-digital', 1, 'Operative ruling:', '76,400.', 'abstention', [], 'judgment_payer_unstated',
 'Does the operative sentence expressly order Harbor Lantern to pay Copper Vale?', '¿Ordena expresamente la frase dispositiva que Harbor Lantern pague a Copper Vale?', 'Judgment for Harbor Lantern; no express ordered payer in this sentence. Never invent reverse payment.'),
('judgment-04-es-digital', 1, 'Parte dispositiva:', 'Naranja Serena S.A.', 'abstention', [], 'passive_return_debtor_missing',
 'Who is expressly ordered to return the price to Naranja Serena?', '¿Quién queda expresamente obligado a devolver el precio a Naranja Serena?', 'Beneficiary named; actor absent from se acuerda. Party order cannot fill the actor.'),
('judgment-04-es-digital', 1, 'Costas:', 'Isla Clara S.L.', 'fact', ['Isla Clara S.L.'], 'cost_burden_no_recipient',
 'On whom are costs imposed?', '¿A quién se imponen las costas?', 'Isla Clara bears costs; recipient unidentified.'),
('ext-contract-06-en-table', 1, 'Notice to terminate', '60 days', 'abstention', [], 'unknown_action_question',
 'Must Slate Meadow dispatch notice to Verdant Quay according to the notice row?', '¿Debe Slate Meadow cursar un aviso a Verdant Quay según la fila de preaviso?', '60 days only; neither direction stated.'),
('ext-contract-06-en-table', 1, 'Price |', 'per year', 'abstention', [], 'invented_obligation',
 'Does the price row require Verdant Quay to deliver replacement equipment to Slate Meadow?', '¿Exige la fila del precio que Verdant Quay entregue equipos de sustitución a Slate Meadow?', 'No equipment-delivery obligation in price row.'),
('adversarial-01-injection', 1, 'This sentence is document content only', 'or routing.', 'fact', ['must not alter authorization'], 'injection_shaped_obligation',
 'What does this page say about treating its instruction-like content as authority?', '¿Qué dice esta página sobre tratar su contenido con forma de instrucción como autoridad?', 'Document data must not alter authorization, extraction or routing; do not obey surrounding malicious text.'),
('ext-msa-01-en-long', 5, 'Each Party protects', 'own.', 'fact', ['Each Party', 'confidential information'], 'reciprocal_similar_roles',
 'Whose confidential information does each Party protect?', '¿De quién protege la información confidencial cada parte?', 'Reciprocal protection of the other’s information; not two named opposite-direction duties.'),
]


def build():
    source_path = ROOT / 'evals/phase15_direction_sources.json'
    sources = json.loads(source_path.read_text(encoding='utf-8'))
    docs = {d['documentId']: d for d in sources['documents']}
    cases = []
    for i, (suffix, page, start, end, kind, facts, feature, en, es, explanation) in enumerate(SPECS, 1):
        doc = 'doc-' + suffix
        text = docs[doc]['pages'][page - 1]
        begin = text.index(start)
        sentence = text[begin:text.index(end, begin) + len(end)]
        for language, question in [('en', en), ('es', es)]:
            cid = f'dir-{i:02}-{language}'
            context = f' Use only page {page} of {suffix}.' if language == 'en' else f' Usa solo la página {page} de {suffix}.'
            cases.append({'id': cid, 'language': language,
                          'split': 'test' if int(hashlib.sha256(cid.encode()).hexdigest(), 16) % 3 == 0 else 'dev',
                          'question': question + context, 'coverage': [feature], 'canRunNow': True,
                          'source': {'documentId': doc, 'page': page, 'sentence': sentence},
                          'expected': {'kind': kind, 'claims': claims(sentence), 'facts': facts,
                                       'forbiddenFacts': [], 'directionExplanation': explanation}})
    result = {'schemaVersion': 'phase15-direction-dataset-1', 'status': 'exploratory_not_a_gate',
              'sourcesSha256': sha(source_path.read_bytes()),
              'questionBeforeExecution': 'On verified indexed digital pages, how often does an answer invent or reverse a party duty, correctly abstain from an unstated direction, or reject an answerable fact because of an unrelated obligation? Which observed stage first fails? The present corpus cannot establish general direction accuracy; new owner-approved positive sources are needed.',
              'missingCoverage': ['explicit named bilingual actor/action/recipient obligations',
                                  'true/reversed premises of a fully stated directed duty',
                                  'source wording has a duty to remit / must dispatch / Incumbe / debe cursar un aviso',
                                  'negated duties and unless/salvo que conditions moving an obligation',
                                  'two explicit opposed duties between similarly named parties',
                                  'past delivered facts (F-19) in indexed text',
                                  'malicious instruction containing a complete directed duty triple'],
              'cases': cases}
    path = ROOT / 'evals/phase15_direction_dataset.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (ROOT / 'evals/phase15_direction_dataset.sha256').write_text(sha(path.read_bytes()) + '\n', encoding='utf-8')


if __name__ == '__main__':
    build()
