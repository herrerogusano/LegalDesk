"""Phase 15: inventory block (pure "which documents are there?" questions)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from legaldesk import agent_contracts as contracts
from legaldesk import agent_templates as templates
from legaldesk.agent_output import BlockError, FallbackReason, LoopEvidence, LoopOutcome, OutputPath, resolve_output
from legaldesk.prompts import FileSystemSystemPromptProvider


def listing(*docs):
    return contracts.parse_list_matter_documents_result({"documents": [
        {"documentId": did, "name": name, "mediaType": media, "jurisdiction": None, "documentDate": None,
         "confidentiality": "PUBLIC", "status": status, "fileSizeBytes": 10, "uploadedAt": uploaded}
        for did, name, media, status, uploaded in docs
    ]})


DOCS = (
    ("doc-b", "Beta.pdf", "application/pdf", "INDEXED", "2026-01-02T10:00:00+00:00"),
    ("doc-a", "alfa.txt", "text/plain", "PENDING_INGESTION", "2026-01-03T10:00:00+00:00"),
    ("doc-c", "Gamma.docx", "application/vnd.x", "FAILED", "2026-01-01T10:00:00+00:00"),
)


def run(block, *, intro=None, inventory=None, tool_use_id="tu-list"):
    evidence = LoopEvidence.from_results(inventories=[(tool_use_id, inventory or listing(*DOCS))])
    plan = {"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": tool_use_id}, **block}]}
    if intro:
        plan["intro"] = intro
    return resolve_output(loop=LoopOutcome.COMPLETED, plan=plan, evidence=evidence)


class InventoryRenderTests(unittest.TestCase):
    def test_every_word_comes_from_templates_and_the_tool_result(self) -> None:
        result = run({"order": {"by": "document_name", "direction": "asc"}}, intro="inventory_of_documents")
        self.assertIs(result.path, OutputPath.INVENTORY)
        intro, block = result.blocks
        self.assertEqual(intro.text, "Estos son los documentos del expediente.")
        self.assertEqual([line.text for line in block.groups[0].lines], [
            "alfa.txt: texto, pendiente de indexación",
            "Beta.pdf: PDF, indexado",
            "Gamma.docx: otro formato, con error",
        ])
        self.assertEqual(block.groups[0].title, templates.INVENTORY_TITLE)
        self.assertEqual([item.text for item in block.aggregates], ["Número de documentos: 3"])
        self.assertEqual(result.citations, ())  # metadata listing: no passages to cite
        public = result.public_dict()
        self.assertEqual(public["blocks"][1]["type"], "inventory")
        self.assertNotIn("citationIds", public["blocks"][1])

    def test_status_filter_and_upload_order(self) -> None:
        result = run({"statusIs": ["INDEXED", "FAILED"], "order": {"by": "uploaded_at", "direction": "desc"}})
        lines = [line.text for line in result.blocks[0].groups[0].lines]
        self.assertEqual(lines, ["Beta.pdf: PDF, indexado", "Gamma.docx: otro formato, con error"])
        self.assertEqual(result.blocks[0].aggregates[0].text, "Número de documentos: 2")

    def test_empty_result_is_a_template_not_an_empty_list(self) -> None:
        result = run({"statusIs": ["UPLOADED"]})
        self.assertIs(result.path, OutputPath.INVENTORY)
        self.assertEqual(result.blocks[0].text, templates.NOTICES["inventory_empty"])
        self.assertEqual(result.blocks[0].groups, ())

    def test_document_names_are_data_never_templates(self) -> None:
        hostile = listing(("doc-a", "{status} {document}.pdf", "application/pdf", "INDEXED", "2026-01-01T00:00:00+00:00"))
        result = run({}, inventory=hostile)
        self.assertEqual(result.blocks[0].groups[0].lines[0].text, "{status} {document}.pdf: PDF, indexado")

    def test_unknown_or_repeated_tool_use_is_omitted_and_falls_back(self) -> None:
        evidence = LoopEvidence.from_results(inventories=[("tu-list", listing(*DOCS))])
        plan = {"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": "tu-other"}}]}
        result = resolve_output(loop=LoopOutcome.COMPLETED, plan=plan, evidence=evidence)
        self.assertIs(result.path, OutputPath.RAG_FALLBACK)
        self.assertIs(result.fallback_reason, FallbackReason.NO_VALID_BLOCKS)
        self.assertEqual([item.error for item in result.omitted], [BlockError.UNKNOWN_TOOL_USE_ID])
        twice = {"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": "tu-list"}}] * 2}
        result = resolve_output(loop=LoopOutcome.COMPLETED, plan=twice, evidence=evidence)
        self.assertIs(result.path, OutputPath.INVENTORY)
        self.assertEqual([item.error for item in result.omitted], [BlockError.DUPLICATE_INVENTORY_BLOCK])

    def test_duplicate_inventory_blocks_are_omitted_with_a_closed_reason_even_between_other_blocks(self) -> None:
        from legaldesk.agent_output import _all_errors, parse_answer_plan  # noqa: F401 - parse re-exported for clarity

        evidence = LoopEvidence.from_results(inventories=[("tu-list", listing(*DOCS))])
        block = {"type": "inventory", "source": {"toolUseId": "tu-list"}}
        plan = {"version": 1, "blocks": [block, {**block, "statusIs": ["INDEXED"]}, {"type": "inventory", "source": {"toolUseId": "tu-nope"}}, block]}
        result = resolve_output(loop=LoopOutcome.COMPLETED, plan=plan, evidence=evidence)
        self.assertEqual(len(result.blocks) - len([b for b in result.blocks if b.kind == "notice"]), 1)  # printed once
        self.assertEqual([(o.index, o.error) for o in result.omitted], [
            (1, BlockError.DUPLICATE_INVENTORY_BLOCK), (2, BlockError.UNKNOWN_TOOL_USE_ID), (3, BlockError.DUPLICATE_INVENTORY_BLOCK)])
        # The diagnostic walk reports the same reasons as the renderer.
        self.assertEqual([(i, e) for i, e in _all_errors(contracts.parse_answer_plan(plan), evidence)],
                         [(o.index, o.error) for o in result.omitted])

    def test_inventory_next_to_text_is_a_mixed_answer(self) -> None:
        search = contracts.parse_search_documents_result({
            "answer": "El plazo es de 30 días.", "evidenceStatus": "answerable", "operationStatus": "ok",
            "citations": [{"citationId": "c1", "documentId": "doc-b", "documentName": "Beta.pdf", "pageNumber": 1,
                           "section": None, "passage": "El plazo será de 30 días."}],
        })
        evidence = LoopEvidence.from_results(search_results=[("tu-search", search)], inventories=[("tu-list", listing(*DOCS))])
        plan = {"version": 1, "blocks": [
            {"type": "inventory", "source": {"toolUseId": "tu-list"}},
            {"type": "text", "source": {"toolUseId": "tu-search"}, "transitionBefore": "continue_with_text"},
        ]}
        result = resolve_output(loop=LoopOutcome.COMPLETED, plan=plan, evidence=evidence)
        self.assertIs(result.path, OutputPath.MIXED)
        self.assertEqual([block.kind for block in result.blocks], ["inventory", "transition", "text"])


class InventoryContractTests(unittest.TestCase):
    def test_plan_rejects_anything_outside_the_closed_lists(self) -> None:
        base = {"type": "inventory", "source": {"toolUseId": "tu-1"}}
        bad_blocks = (
            {**base, "statusIs": ["DELETED"]},
            {**base, "statusIs": []},
            {**base, "statusIs": ["INDEXED", "INDEXED"]},
            {**base, "order": {"by": "size", "direction": "asc"}},
            {**base, "order": {"by": "document_name"}},
            {**base, "title": "Aqui tienes todo"},  # free text key
            {**base, "source": {"toolUseId": "tu-1", "matterId": "m"}},
        )
        for block in bad_blocks:
            with self.assertRaises(contracts.AgentContractError, msg=str(block)):
                contracts.parse_answer_plan({"version": 1, "blocks": [block]})

    def test_listing_parser_is_strict_but_ignores_unused_keys(self) -> None:
        self.assertEqual(len(listing(*DOCS).documents), 3)
        row = {"documentId": "d", "name": "n", "mediaType": "x", "status": "INDEXED", "uploadedAt": "t"}
        for bad in (
            {"documents": "nope"}, {"documents": [dict(row, status="weird status")]}, {"documents": [dict(row, status="X" * 40)]},
            {"documents": [dict(row, status="INDEXED\n")]}, {"documents": [dict(row, status="")]}, {"documents": [dict(row, name=" ")]},
            {"documents": [row, row]}, {"documents": [dict(row, documentId="../x")]}, None,
        ):
            with self.assertRaises(contracts.AgentContractError, msg=str(bad)):
                contracts.parse_list_matter_documents_result(bad)

    def test_an_unknown_lifecycle_status_gets_a_generic_label_and_never_raises(self) -> None:
        row = {"documentId": "d", "name": "Nuevo.pdf", "mediaType": "application/pdf", "status": "QUARANTINED", "uploadedAt": "t"}
        inventory = contracts.parse_list_matter_documents_result({"documents": [row]})
        result = run({}, inventory=inventory)
        self.assertEqual(result.blocks[0].groups[0].lines[0].text, "Nuevo.pdf: PDF, estado no reconocido")
        self.assertNotIn("QUARANTINED", result.plain_text())
        filtered = run({"statusIs": ["INDEXED"]}, inventory=inventory)  # filters only know the closed list
        self.assertEqual(filtered.blocks[0].text, templates.NOTICES["inventory_empty"])

    def test_prompt_documents_the_inventory_vocabulary(self) -> None:
        prompt = " ".join(FileSystemSystemPromptProvider(ROOT / "prompts" / "legaldesk-agent.md").load().content.split())
        for phrase in (*contracts.INVENTORY_STATUS_FILTERS, *contracts.INVENTORY_ORDER_BY, "inventory_of_documents"):
            self.assertIn(phrase, prompt)
        self.assertIn("Inventory block", prompt)


if __name__ == "__main__":
    unittest.main()
