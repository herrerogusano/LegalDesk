"""Read-only audit of real PDF extraction; emit counts/closed codes, never text."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT / "evals")]
from phase15_golden_eval import load_dataset, load_manifest
from legaldesk.evidence import _source_sentences, evidence_requires_relationship_projection
from legaldesk.idp.acquisition import acquire_pdf


def audit(prepared_corpus: Path | None = None) -> dict[str, object]:
    dataset, digest = load_dataset()
    fixtures = load_manifest()
    prepared_hashes = {}
    if prepared_corpus is not None:
        prepared_hashes = {d["goldenId"]: d["sha256"] for d in
                           json.loads((prepared_corpus / "corpus-manifest.json").read_text(encoding="utf-8"))["documents"]}
    counts = dict(documents=0, fixtureDocuments=0, preparedDocuments=0, validPDFs=0,
                  expectedInvalidPDFs=0, unexpectedErrors=0, pages=0, ocrRequiredPages=0)
    representations = {
        name: dict(nonemptyPages=0, pagesWithBlankLines=0, obligationPages=0, unsafeObligationSegments=0)
        for name in ("raw", "normalized")
    }
    for item in dataset["world"]["documents"]:
        if "fixtureId" in item:
            fixture = fixtures[item["fixtureId"]]
            path, expected = ROOT / "tests/fixtures/idp/pdfs" / fixture["filename"], fixture["sha256"]
            counts["fixtureDocuments"] += 1
        elif prepared_corpus is not None:
            path, expected = prepared_corpus / item["name"], prepared_hashes[item["documentId"]]
            counts["preparedDocuments"] += 1
        else:
            continue
        counts["documents"] += 1
        try:
            content = path.read_bytes()
        except OSError:
            counts["unexpectedErrors"] += 1
            continue
        if hashlib.sha256(content).hexdigest() != expected:
            counts["unexpectedErrors"] += 1
            continue
        # Suppress library diagnostics for the deliberately truncated fixture;
        # only classified counts leave this read-only diagnostic.
        logger = logging.getLogger("pypdf")
        previous_disabled = logger.disabled
        try:
            logger.disabled = True
            with contextlib.redirect_stderr(io.StringIO()):
                document = acquire_pdf(content, expected_sha256=expected)
        except Exception:
            key = "expectedInvalidPDFs" if item["status"] == "FAILED" else "unexpectedErrors"
            counts[key] += 1
            continue
        finally:
            logger.disabled = previous_disabled
        counts["validPDFs"] += 1
        counts["pages"] += document.page_count
        counts["ocrRequiredPages"] += len(document.ocr_required_pages)
        for page in document.pages:
            for name, text in (("raw", page.text), ("normalized", page.normalized_text)):
                measured = representations[name]
                measured["nonemptyPages"] += bool(text.strip())
                measured["pagesWithBlankLines"] += bool(text.count("\n\n"))
                measured["obligationPages"] += evidence_requires_relationship_projection([{"text": text}])
                measured["unsafeObligationSegments"] += sum(
                    not safe and evidence_requires_relationship_projection([{"text": sentence}])
                    for sentence, safe in _source_sentences(text)
                )
    return {"datasetSha256": digest, "counts": counts, "representations": representations,
            "liveOCRCalls": 0, "modelCalls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-corpus", type=Path)
    measured = audit(parser.parse_args().prepared_corpus)
    print(json.dumps(measured, sort_keys=True, indent=2))
    raise SystemExit(1 if measured["counts"]["unexpectedErrors"] else 0)
