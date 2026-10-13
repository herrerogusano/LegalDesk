"""Offline, metadata-only triage of IDP value-digest mismatches.

Re-runs ``scripts/evaluate_idp.py`` over the immutable real-model exports in the
documented supersession order, then triages every present field whose value
digest differs from the fixture oracle.  Exports are read, never rewritten.

Digest-only exports cannot reveal a value.  For registry fields whose value
domain is a closed server-owned vocabulary (``duration_unit``), the raw
spelling is recovered by digest lookup over that vocabulary and replayed
through the *current* canonicalizer and extractor parser against the
synthetic fixture page text.  The replay only runs when every exported anchor
matches the fixture oracle ``(page, quoteDigest)`` pair, so no evidence check
is relaxed.  The report carries closed-vocabulary unit labels and digests only;
it never emits document text, quotes, or free-form values.

Exit status is non-zero when any ``AUTO_ACCEPTED`` value mismatch remains
unresolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend" / "src"))

import evaluate_idp as ev  # noqa: E402
from legaldesk.idp import FieldAcceptance, IDPSchemaRegistry, parse_extractor_output  # noqa: E402
from legaldesk.idp.processing import _DURATION_UNIT_ALIASES, _canonical_duration_unit  # noqa: E402

# Later entries supersede earlier ones for the same fixture, matching the
# selection policy of ``idp-consolidated-20261009-j.json``.
SELECTION_ORDER = (
    "idp-canary-contract-20261009-compact-e.json",
    "idp-digital-corpus-20261009-f.json",
    "idp-ocr-corpus-20261009-h.json",
    "idp-ocr-targeted-20261009-i.json",
    "idp-ocr-targeted-20261009-j.json",
)


def _closed_unit_vocabulary() -> dict[str, str]:
    spellings = set(_DURATION_UNIT_ALIASES) | set(_DURATION_UNIT_ALIASES.values())
    variants = {variant for spelling in spellings for variant in (spelling, spelling.lower(), spelling.upper(), spelling.capitalize())}
    return {ev._digest(variant): variant for variant in sorted(variants)}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replay(raw_fixture: Mapping[str, Any], document_type: str, field_name: str, spelling: str) -> dict[str, Any]:
    """Replay the recovered spelling with the fixture's own oracle quotes."""

    expected = raw_fixture["expected"]["fields"][field_name]
    pages = {index: page["text"] for index, page in enumerate(raw_fixture["source_pages"], start=1)}
    evidence = [{"page": anchor["page"], "quote": anchor["quote"]} for anchor in expected.get("evidence", ())]
    content_sha256 = raw_fixture["sha256"]
    schema = IDPSchemaRegistry().get(document_type)
    result = parse_extractor_output(
        json.dumps({"fields": {field_name: {"presence": "PRESENT", "value": spelling, "evidence": evidence}}}),
        schema=schema,
        page_text=pages,
        content_sha256=content_sha256,
    ).fields[field_name]
    return {
        "value_digest_matches_fixture": ev._digest(result.value) == ev._expected_field(expected)["value_digest"],
        "acceptance": result.acceptance.value,
        "origin": result.origin.value if result.origin is not None else None,
        "anchor_found": bool(result.validation.get("anchor_found")),
        "lexical_support": bool(result.validation.get("lexical_support")),
    }


def triage(manifest_path: Path, results_dir: Path) -> dict[str, Any]:
    manifest = ev.load_manifest(manifest_path)
    raw_manifest = ev._load_json(manifest_path, "manifest")
    raw_by_id = {fixture["id"]: fixture for fixture in raw_manifest["fixtures"]}
    expected_by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
    retained: dict[str, tuple[str, Mapping[str, Any]]] = {}
    sources = []
    for name in SELECTION_ORDER:
        path = results_dir / name
        sources.append({"file": name, "sha256": _file_sha256(path)})
        report = ev.evaluate(manifest, ev._load_json(path, "results export"))
        for case in report["cases"]:
            if case["status"] == "OBSERVED":
                retained[case["fixture_id"]] = (name, case)

    vocabulary = _closed_unit_vocabulary()
    registry = IDPSchemaRegistry()
    items: list[dict[str, Any]] = []
    for fixture_id in sorted(retained):
        source, case = retained[fixture_id]
        document_type = expected_by_id[fixture_id]["expected_type"]
        for field_name, field in case["fields"].items():
            if field["value_match"] != "FAIL":
                continue
            try:
                spec = registry.get(document_type).field(field_name)
                lexical_kind = spec.lexical_kind
            except Exception:
                lexical_kind = "unregistered"
            anchor_errors = sorted(set(field["evidence"]["errors"]) & {"ORACLE_ANCHOR_MISMATCH", "EVIDENCE_MISSING", "EVIDENCE_CONTENT_HASH_MISMATCH", "EVIDENCE_CONTENT_HASH_NOT_FIXTURE", "EVIDENCE_PAGE_OUT_OF_RANGE"})
            item: dict[str, Any] = {
                "fixture_id": fixture_id, "field": field_name, "source": source, "lexical_kind": lexical_kind,
                "observed_acceptance": field["actual_acceptance"], "observed_origin": field["actual_origin"],
                "observed_value_digest": field["value_digest"], "evaluator_errors": field["evidence"]["errors"],
            }
            spelling = vocabulary.get(field["value_digest"] or "") if lexical_kind == "duration_unit" else None
            if spelling is None:
                item["outcome"] = "UNRESOLVED_DIGEST_ONLY_AUTO_ACCEPTED" if field["actual_acceptance"] == FieldAcceptance.AUTO_ACCEPTED.value else "UNRESOLVED_DIGEST_ONLY_ROUTED"
                items.append(item)
                continue
            canonical = _canonical_duration_unit(spelling)
            item["recovered_unit_spelling"] = spelling
            item["current_canonical_unit"] = canonical
            item["canonical_digest_matches_fixture"] = canonical is not None and ev._digest(canonical) == expected_by_id[fixture_id]["fields"][field_name]["value_digest"]
            if anchor_errors:
                item["replay"] = {"status": "NOT_RUN", "reason": "exported anchor is not the fixture oracle; quote cannot be recovered from digests"}
                item["outcome"] = "VALUE_CANONICALIZED_ANCHOR_UNVERIFIED_ROUTED" if field["actual_acceptance"] != FieldAcceptance.AUTO_ACCEPTED.value else "UNRESOLVED_ANCHOR_AUTO_ACCEPTED"
            else:
                replay = _replay(raw_by_id[fixture_id], document_type, field_name, spelling)
                item["replay"] = {"status": "RUN", **replay}
                resolved = item["canonical_digest_matches_fixture"] and replay["value_digest_matches_fixture"] and replay["anchor_found"]
                item["outcome"] = "RESOLVED_BY_CANONICALIZATION" if resolved else "UNRESOLVED_REPLAY_MISMATCH"
            items.append(item)

    def count(predicate) -> int:
        return sum(1 for item in items if predicate(item))

    unresolved_auto = count(lambda item: item["outcome"].startswith("UNRESOLVED") and item["observed_acceptance"] == FieldAcceptance.AUTO_ACCEPTED.value)
    return {
        "report_schema_version": "1.0-value-mismatch-triage",
        "evaluator": "scripts/evaluate_idp.py",
        "triage_tool": "scripts/triage_idp_value_mismatches.py",
        "manifest": {"path": "tests/fixtures/idp/manifest.json", "dataset_id": manifest.get("dataset_id"), "fixture_count": len(manifest["fixtures"])},
        "generated_from": sources,
        "selection_policy": "later export in generated_from supersedes an earlier one for the same fixture (consolidated-j policy)",
        "summary": {
            "present_value_mismatches": len(items),
            "observed_auto_accepted_value_mismatches": count(lambda item: item["observed_acceptance"] == FieldAcceptance.AUTO_ACCEPTED.value),
            "resolved_by_canonicalization": count(lambda item: item["outcome"] == "RESOLVED_BY_CANONICALIZATION"),
            "value_canonicalized_anchor_unverified_routed": count(lambda item: item["outcome"] == "VALUE_CANONICALIZED_ANCHOR_UNVERIFIED_ROUTED"),
            "unresolved_routed": count(lambda item: item["outcome"] == "UNRESOLVED_DIGEST_ONLY_ROUTED"),
            "unresolved_auto_accepted": unresolved_auto,
            "auto_acceptance_blocker": unresolved_auto > 0,
        },
        "items": items,
        "limitations": [
            "Metadata-only: no document text, quotes, prompts, or free-form values are emitted; unit labels come from the closed registry vocabulary.",
            "Replays use the current canonicalizer and parser on fixture page text; they do not re-call any model and do not rewrite the immutable exports.",
            "Non-unit mismatches remain digest-only; they are routed (not AUTO_ACCEPTED) and still require source-backed field review.",
        ],
    }


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests" / "fixtures" / "idp" / "manifest.json")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "evals" / "results")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = triage(args.manifest, args.results_dir)
    except ev.EvaluationError as exc:
        print(f"triage failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    return 1 if report["summary"]["auto_acceptance_blocker"] else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
