"""Offline F-35 census. Emits metadata/counts only; no provider calls or text."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT / "evals"), str(ROOT / "tests")]
from phase15_golden_eval import build_world, load_dataset, load_manifest
from legaldesk.evidence import evidence_requires_relationship_projection
from test_relationship_source_rendering import run

# Independent document-level construction annotation, not copied from detector
# output: future-payment order, modal-directed notice, future-return lease.
# Negative/field-only Spanish sources are not directed obligation sentences.
DIRECTED_SPANISH_DOCUMENTS = frozenset({
    "doc-judgment-02-es-scanned", "doc-ext-notice-01-es-short", "doc-ext-lease-01-es-long",
})


def baseline_requires_projection(text):
    # Exact pre-F-35 ASCII/English trigger, retained only for offline comparison.
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    modals = {"must", "shall", "required", "responsible", "obliged", "obligated", "may", "should"}
    actions = set("send sending deliver delivering provide providing give giving issue issuing serve serving notify notifying pay paying submit submitting transfer transferring disclose disclosing report reporting approve approving owe owing assign assigning indemnify indemnifying reimburse reimbursing return returning remit remitting furnish furnishing supply supplying grant granting release releasing convey conveying lend lending lease leasing".split())
    return any(token in modals and any(tokens[j] in actions and j + 1 < len(tokens) for j in range(i + 1, len(tokens)))
               for i, token in enumerate(tokens))


def census():
    dataset, digest = load_dataset()
    world = build_world(dataset)
    spanish = [d for d in world.values() if d.language == "es" and d.pages]
    positives = set()
    baseline = set()
    cohorts = {
        "nonObligation": {"prose": "Synthetic offline census date: 3 March.", "missing": 0, "malformed": 0, "dropReasons": 0},
        "assertedObligation": {"prose": "El proveedor debe notificar al comprador.", "missing": 0, "malformed": 0, "dropReasons": 0},
    }
    unexpected_errors = 0
    for doc in spanish:
        text = "\n\n".join(doc.pages)
        if evidence_requires_relationship_projection([{"text": page} for page in doc.pages]):
            positives.add(doc.document_id)
        if baseline_requires_projection(text):
            baseline.add(doc.document_id)
        # Real Converse resolver/writer + grounding/chat; only Bedrock clients
        # faked. These are controlled missing/malformed outputs, not model runs.
        for cohort in cohorts.values():
            for variant, relationships, expected in (
                ("missing", [], "writer_relationship_missing"),
                ("malformed", [{"actor": "synthetic"}], "writer_relationship_shape_invalid"),
            ):
                response, events, _, _ = run(text, relationships, prose=cohort["prose"])
                terminal = [e for e in events.events if e.operation and e.operation.value == "write_answer" and e.outcome.value != "started"]
                rejected = [e for e in terminal if e.outcome.value == "error"]
                matched = response.error_code == "model_failed" and len(rejected) == 1 and rejected[0].error_code.value == expected
                cohort[variant] += matched
                dropped = response.operation_status == "ok" and len(terminal) == 1 and terminal[0].error_code and terminal[0].error_code.value == expected
                if variant == "malformed":
                    cohort["dropReasons"] += bool(dropped)
                unexpected_errors += response.operation_status != "ok" and not matched
    fixtures = [f for f in load_manifest().values() if f["language"] == "es"]
    fixture_positive = sum(evidence_requires_relationship_projection([{"text": p["text"]} for p in f["source_pages"]]) for f in fixtures)
    return {
        "datasetSha256": digest,
        "corpusDocuments": len(world),
        "spanishFilenameDocuments": sum("-es-" in d.name for d in world.values()),
        "spanishLanguageDocuments": sum(d.language == "es" for d in world.values()),
        "nonemptySpanishDocuments": len(spanish),
        "spanishFixtureDocuments": len(fixtures),
        "spanishFixtureDetected": fixture_positive,
        "annotatedDirectedDocuments": len(DIRECTED_SPANISH_DOCUMENTS),
        "baselineDetected": len(baseline),
        "detected": len(positives),
        "falsePositives": len(positives - DIRECTED_SPANISH_DOCUMENTS),
        "falseNegatives": len(DIRECTED_SPANISH_DOCUMENTS - positives),
        "missingProjectionAnswersFailClosed": cohorts["nonObligation"]["missing"],
        "malformedProjectionAnswersFailClosed": cohorts["nonObligation"]["malformed"],
        "nonObligationMalformedAcceptedWithDropReason": cohorts["nonObligation"]["dropReasons"],
        "assertedObligationMissingProjectionAnswersFailClosed": cohorts["assertedObligation"]["missing"],
        "assertedObligationMalformedProjectionAnswersFailClosed": cohorts["assertedObligation"]["malformed"],
        "assertedObligationMalformedAcceptedWithDropReason": cohorts["assertedObligation"]["dropReasons"],
        "controlledRequests": len(spanish) * len(cohorts) * 2,
        "unexpectedErrors": unexpected_errors,
        "detectedDocumentIds": sorted(positives),
        "liveModelAnswersMeasured": 0,
    }


if __name__ == "__main__":
    print(json.dumps(census(), sort_keys=True, indent=2))
