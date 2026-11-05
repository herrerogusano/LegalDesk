"""F-50: the real-mode oracles, calibrated on the eight real answers read by hand on 2026-11-05.

The shapes below are what the deployed pipeline really returned (real page number lost, partial abstentions with a citation,
extra lines in a mixed answer). Every relaxed rule also has a case that must still fail: an oracle that cannot fail proves nothing.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
for part in (".", "backend/src", "agent/src", "tests"):
    sys.path.insert(0, str(ROOT / part))

from evals import phase15_golden_eval as ev

FROZEN, DIGEST = ev.load_dataset()
OVERLAY, OVERLAY_DIGEST = ev.load_real_overlay()
DATASET = ev.apply_real_overlay(FROZEN, DIGEST, OVERLAY)
WORLD = ev.build_world(DATASET)
CASES = {case["id"]: case for case in DATASET["cases"]}
NORTHSTAR = "doc-contract-01-en-digital-monthend"


def text_block(text, status, citation_ids=("c1",)):
    return {"type": "text", "text": text, "evidenceStatus": status, "citationIds": list(citation_ids)}


def citation(document_id, citation_id="c1"):
    return {"citationId": citation_id, "documentId": document_id, "pageNumber": None}   # the real page is lost: F-50


def observation(case_id, output, *, tools, path, retrieved=(), status=None, supporting=(), probe_citations=(), idp=None):
    case = CASES[case_id]
    public = dict(output, path=path, operationStatus="ok")
    record = None
    if case["expected"].get("rag"):
        record = ev.PipelineRecord(
            retrieved=[(d, 1) for d in retrieved], resolver_ran=True, resolver_status=status, supporting=[(d, 1) for d in supporting], writer_ran=True,
            writer_facts_ok=True, writer_forbidden=False, grounding="ok",
            final={"operationStatus": "ok", "evidenceStatus": status, "citations": [(d, None) for d in probe_citations], "factsOk": True, "forbiddenHit": False})
    return ev.GoldenObservation(agent_ran=True, called_tools=list(tools), path=path, output=public, final=ev.build_final(case, WORLD, public), pipeline=record, idp=idp or {})


def data_line(document_id, field, *, pending=False):
    value = ev.agent_output.render_value(WORLD[document_id].fields[field].value)
    return {"text": f"{WORLD[document_id].name}: {field}: {value}", "pending": pending, "citationIds": [], "documentId": document_id, "fieldName": field}


def lt_01(*, cited=NORTHSTAR, answer="El contrato exige un preaviso de treinta (30) días mediante notificación escrita."):
    return observation("lt-01", {"blocks": [text_block(answer, "answerable")], "citations": [citation(cited)]}, tools=["search_documents"], path="grounded",
                       retrieved=[NORTHSTAR, "doc-ext-msa-01-en-long"], status="answerable", supporting=[NORTHSTAR], probe_citations=[cited])


def un_04(*, answer="La evidencia indica que dictó la sentencia el tribunal, pero no se especifica el nombre.", cited="doc-judgment-01-en-digital"):
    return observation("un-04", {"blocks": [text_block(answer, "insufficient_evidence")], "citations": [citation(cited)]}, tools=["search_documents"], path="grounded",
                       retrieved=["doc-judgment-04-es-digital", "doc-judgment-01-en-digital"], status="insufficient_evidence", probe_citations=[cited])


def id_03(ids=("doc-contract-04-es-digital-amount", "doc-ext-msa-01-en-long", "doc-ext-lease-01-es-long", "doc-ext-contract-07-es-annex")):
    block = {"type": "data", "groups": [{"title": "t", "lines": [data_line(d, "effective_date") for d in ids]}], "aggregates": []}
    return observation("id-03", {"blocks": [block], "citations": []}, tools=["list_matter_documents", "get_extracted_fields"], path="data",
                       idp={d: {"type": WORLD[d].doc_type, "fields": {"effective_date": {"status": "fact", "value": WORLD[d].fields["effective_date"].value}}} for d in ids})


def mx_01(extra):
    lines = [data_line(NORTHSTAR, "effective_date"), extra]
    blocks = [{"type": "data", "groups": [{"title": "t", "lines": lines}], "aggregates": []},
              text_block("El contrato exige un preaviso de treinta (30) días.", "answerable")]
    return observation("mx-01", {"blocks": blocks, "citations": [citation(NORTHSTAR)]}, tools=["list_matter_documents", "get_extracted_fields", "search_documents"], path="mixed",
                       retrieved=[NORTHSTAR], status="answerable", supporting=[NORTHSTAR], probe_citations=[NORTHSTAR])


def inventory(case_id, names):
    lines = [{"text": f"{WORLD[d].name}: PDF, indexado", "pending": False, "citationIds": []} for d in names]
    return observation(case_id, {"blocks": [{"type": "inventory", "groups": [{"title": "t", "lines": lines}], "aggregates": []}], "citations": []},
                       tools=["list_matter_documents"], path="inventory")


class RealAnswersReadByHand(unittest.TestCase):
    def assert_passes(self, case_id, obs):
        row = ev.evaluate_case(CASES[case_id], obs, WORLD)
        self.assertEqual((row["outcome"], row["firstFailure"], row["finalCorrect"]), ("pass", None, True), row["phases"])

    def assert_wrong(self, case_id, obs):
        self.assertFalse(ev.evaluate_case(CASES[case_id], obs, WORLD)["finalCorrect"])

    def test_the_answers_read_as_right_now_pass(self) -> None:
        self.assert_passes("lt-01", lt_01())
        self.assert_passes("un-04", un_04())
        self.assert_passes("id-03", id_03())
        self.assert_passes("mx-01", mx_01(data_line(NORTHSTAR, "termination_notice_value", pending=True)))
        self.assert_passes("inv-02", inventory("inv-02", ev.expected_inventory(CASES["inv-02"], WORLD)))

    def test_a_citation_to_the_wrong_document_still_fails(self) -> None:
        self.assert_wrong("lt-01", lt_01(cited="doc-ext-msa-01-en-long"))

    def test_a_missing_fact_still_fails(self) -> None:
        self.assert_wrong("lt-01", lt_01(answer="El contrato exige un preaviso."))

    def test_an_abstention_with_an_invented_detail_still_fails(self) -> None:
        self.assert_wrong("un-04", un_04(answer="La sentencia la dictó el magistrado Pérez."))

    def test_a_citation_to_a_passage_that_was_not_retrieved_still_fails(self) -> None:
        row = ev.evaluate_case(CASES["un-04"], un_04(cited="doc-ext-msa-01-en-long"), WORLD)
        self.assertEqual(row["phases"]["citations"], "fail")
        self.assertFalse(row["finalCorrect"])

    def test_the_resolver_may_only_support_an_abstention_with_what_was_retrieved(self) -> None:
        obs = un_04()
        obs.pipeline.supporting = [("doc-judgment-01-en-digital", 1)]
        self.assertEqual(ev.evaluate_case(CASES["un-04"], obs, WORLD)["phases"]["resolver"], "pass")
        obs = un_04()
        obs.pipeline.supporting = [("doc-ext-msa-01-en-long", 1)]
        self.assertEqual(ev.evaluate_case(CASES["un-04"], obs, WORLD)["phases"]["resolver"], "fail")

    def test_a_missing_or_extra_contract_still_fails(self) -> None:
        self.assert_wrong("id-03", id_03(ids=("doc-contract-04-es-digital-amount", "doc-ext-msa-01-en-long", "doc-ext-lease-01-es-long")))

    def test_an_extra_line_must_be_right_and_on_the_same_document(self) -> None:
        bad_value = dict(data_line(NORTHSTAR, "termination_notice_value", pending=True), text="contract-01.pdf: preaviso: 999999")
        self.assert_wrong("mx-01", mx_01(bad_value))
        self.assert_wrong("mx-01", mx_01(data_line("doc-contract-04-es-digital-amount", "effective_date")))

    def test_an_extra_line_is_only_tolerated_in_mixed_cases(self) -> None:
        ids = ("doc-contract-04-es-digital-amount", "doc-ext-msa-01-en-long", "doc-ext-lease-01-es-long", "doc-ext-contract-07-es-annex", NORTHSTAR)
        self.assert_wrong("id-03", id_03(ids=ids))

    def test_a_scanned_document_in_the_indexed_list_fails_and_the_real_list_passes(self) -> None:
        wanted = CASES["inv-02"]["expected"]["inventory"]["documentIds"]
        self.assertEqual(len(wanted), 19)
        self.assert_wrong("inv-02", inventory("inv-02", wanted + ["doc-contract-02-es-scanned-leapday"]))
        self.assertEqual(len(CASES["inv-03"]["expected"]["inventory"]["documentIds"]), 7)


class TheOverlayIsBounded(unittest.TestCase):
    def test_it_only_loads_on_the_dataset_it_was_written_for(self) -> None:
        with self.assertRaises(ValueError):
            ev.apply_real_overlay(FROZEN, "0" * 64, OVERLAY)

    def test_the_frozen_dataset_and_the_questions_are_untouched(self) -> None:
        self.assertEqual(DIGEST, ev.EXPECTED_DATASET_SHA256)
        self.assertEqual(OVERLAY_DIGEST, ev.EXPECTED_OVERLAY_SHA256)
        before = copy.deepcopy(FROZEN)
        ev.apply_real_overlay(FROZEN, DIGEST, OVERLAY)
        self.assertEqual(FROZEN, before)
        self.assertEqual([c["question"] for c in FROZEN["cases"]], [c["question"] for c in DATASET["cases"]])
        self.assertEqual([c["expected"]["route"] for c in FROZEN["cases"]], [c["expected"]["route"] for c in DATASET["cases"]])

    def test_every_case_change_says_why(self) -> None:
        for case_id, change in OVERLAY["cases"].items():
            if set(change) - {"extraLinesOnSameDocuments", "knownLimit"}:
                self.assertIn("reason", change, case_id)
            if "knownLimit" in change:
                self.assertEqual(change["knownLimit"]["basis"], "F-31", case_id)

    def test_field_status_only_moves_in_the_prudent_direction(self) -> None:
        frozen_world = ev.build_world(FROZEN)
        self.assertTrue(OVERLAY["fieldsPendingReview"])
        for document_id, field in OVERLAY["fieldsPendingReview"]:
            self.assertEqual(frozen_world[document_id].fields[field].status, "fact")
            self.assertEqual(WORLD[document_id].fields[field].status, "pending_review")
            self.assertEqual(frozen_world[document_id].fields[field].value, WORLD[document_id].fields[field].value)

    def test_a_pending_field_shown_as_a_fact_is_a_failure(self) -> None:
        document_id, field = next((d, f) for d, f in OVERLAY["fieldsPendingReview"] if d == NORTHSTAR and f == "governing_law")
        case = copy.deepcopy(CASES["id-03"])
        case["expected"]["data"] = {"refs": [[document_id, field]], "aggregates": []}
        output = {"path": "data", "operationStatus": "ok", "citations": [], "blocks": [{"type": "data", "groups": [{"title": "t", "lines": [data_line(document_id, field, pending=False)]}], "aggregates": []}]}
        obs = ev.GoldenObservation(agent_ran=True, called_tools=["list_matter_documents", "get_extracted_fields"], path="data", output=output, final=ev.build_final(case, WORLD, output))
        self.assertFalse(ev.answer_is_correct(case, obs, WORLD))


class TheRecorderGradesTheRealMatter(unittest.TestCase):
    RESULTS = {"retrievalResults": [
        {"content": {"text": "a"}, "metadata": {"tenantId": "tnt_real", "matterId": "mat_real", "documentId": "R", "x-amz-bedrock-kb-document-page-number": 1.0}},
        {"content": {"text": "b"}, "metadata": {"tenantId": "tnt_real", "matterId": "mat_other", "documentId": "S", "x-amz-bedrock-kb-document-page-number": 1.0}}]}

    def test_it_records_the_run_matter_and_not_the_simulated_one(self) -> None:
        record = ev.PipelineRecord()
        ev.RecordingKB(SimpleNamespace(retrieve=lambda **kw: self.RESULTS), record, {"R": "doc-r"}, matter_id="mat_real", tenant_id=None).retrieve()
        self.assertEqual(record.retrieved, [("doc-r", 1)])

    def test_the_real_pipeline_passes_its_matter_to_the_recorder(self) -> None:
        service = SimpleNamespace(retrieval_client=SimpleNamespace(retrieve=lambda **kw: self.RESULTS), evidence_resolver=None, answer_writer=None, grounding_validator=None)
        with patch.object(dataclasses, "replace", side_effect=lambda s, **kw: SimpleNamespace(**kw)):
            run = ev.make_real_pipeline(CASES["lt-01"], service, None, {"R": "doc-r"}, subject="s", matter_id="mat_real")
        run.service.retrieval_client.retrieve()
        self.assertEqual(run.record.retrieved, [("doc-r", 1)])


class TheReportCanBeReJudged(unittest.TestCase):
    def test_the_row_keeps_ids_and_never_the_answer(self) -> None:
        row = ev.evaluate_case(CASES["lt-01"], lt_01(answer="ANSWER-TEXT-MUST-NOT-APPEAR treinta (30) días"), WORLD)
        self.assertEqual([tuple(p) for p in row["observed"]["textCitations"]], [(NORTHSTAR, None)])
        self.assertIn(NORTHSTAR, row["observed"]["retrieved"])
        self.assertNotIn("ANSWER-TEXT", json.dumps(row, default=str))

    def test_an_answer_in_the_wrong_language_is_reported(self) -> None:
        english = "The evidence establishes that the judgment was issued by the court, but the name of the judge is not specified."
        spanish = "La evidencia indica que la sentencia es del tribunal, pero no se especifica el nombre del juez."
        self.assertTrue(ev.evaluate_case(CASES["un-04"], un_04(answer=english), WORLD)["languageMismatch"])
        self.assertFalse(ev.evaluate_case(CASES["un-04"], un_04(answer=spanish), WORLD)["languageMismatch"])


if __name__ == "__main__":
    unittest.main()
