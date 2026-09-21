"""Deterministic grounding oracle for the bounded Phase 12 fixtures.

The production request pipeline deliberately does not use this module. It is a local,
fixture-owned acceptance oracle for the bounded evaluation: each fixture
declares the claims that an answer must preserve instead of declaring one
canonical wording.  This keeps evaluation semantics separate from writer
phrasing while retaining fail-closed citation and unsupported-value checks.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


_DATE = re.compile(r"\b(?P<year>\d{4})[-/]?(?P<month>\d{1,2})[-/]?(?P<day>\d{1,2})\b")
_DATE_DMY = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>january|february|march|april|may|june|"
    r"july|august|september|october|november|december)\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_DATE_MDY = re.compile(
    r"\b(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r",?\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?:(?:€|eur(?:o)?s?)\s*(?P<prefix>[0-9]+(?:[.,][0-9]+)?)|"
    r"(?P<suffix>[0-9]+(?:[.,][0-9]+)?)\s*(?:€|eur(?:o)?s?))",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"\b(?P<number>[0-9]+(?:[.,][0-9]+)?|"
    r"twenty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"thirty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"forty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"fifty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"sixty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"seventy(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"eighty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"ninety(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s*[- ]?\s*(?P<unit>[a-z]+)",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[\w]+", re.UNICODE)

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_UNIT_ALIASES = {
    "day": "day",
    "days": "day",
    "unit": "unit",
    "units": "unit",
    "week": "week",
    "weeks": "week",
    "month": "month",
    "months": "month",
    "year": "year",
    "years": "year",
    "item": "item",
    "items": "item",
}
_UNKNOWN_MARKERS = (
    "not established",
    "not fully established",
    "not specified",
    "not stated",
    "not provided",
    "not available",
    "cannot determine",
    "cannot be determined",
    "can't determine",
    "unknown",
    "undetermined",
    "insufficient information",
    "does not establish",
    "do not establish",
    "does not say",
    "do not say",
)
_EVIDENCE_ABSENCE_MARKERS = _UNKNOWN_MARKERS + (
    "omit",
    "omits",
    "omitted",
    "without specifying",
    "without stating",
    "does not specify",
    "doesn't specify",
    "lacks",
    "missing",
)
_NEGATION_TERMS = {
    "not", "no", "never", "cannot", "cant",
    "isnt", "arent", "wasnt", "werent", "doesnt", "dont",
    # Apostrophes are token separators under ``_normalize``.
    "isn", "aren", "wasn", "weren", "doesn", "don",
}
_TYPED_VALUE_KINDS = {"date", "money", "quantity"}
_SAFE_LEXICAL_TOKENS = {
    "a", "an", "and", "according", "applicable", "applies", "are", "as",
    "at", "available", "be", "been", "being", "but", "by", "can", "cannot",
    "clause", "could", "detail", "determined", "directly", "do", "document",
    "documented", "documents", "does", "establish", "established", "evidence",
    "for", "from", "fully", "had", "has", "have", "however", "in", "indicate",
    "indicates", "information", "insufficient", "is", "it", "its", "known",
    "material", "may", "mention", "mentioned", "mentions", "must", "no", "not",
    "of", "on", "only", "or", "passage", "passages", "provided", "related",
    "relevant", "remains", "requested", "s", "say", "says", "selected", "scope", "should",
    "specified", "state", "stated", "states", "that", "the", "their", "these",
    "they", "this", "those", "to", "total", "period", "unknown", "was", "were", "what", "while",
    "with", "would",
    *_MONTHS.keys(),
    *_NUMBER_WORDS.keys(),
}
_FORBIDDEN_MATCH_STOPWORDS = {"a", "all", "an", "and", "as", "of", "or", "the", "to"}
_DIRECTIVE_TOKEN_ALIASES = {
    "bypass": "ignore",
    "disclose": "reveal",
    "guardrails": "safeguards",
    "info": "data",
    "information": "data",
    "protections": "safeguards",
}


@dataclass(frozen=True, slots=True)
class GroundingClaim:
    """One fixture-declared semantic constraint.

    ``required_values`` are values that the answer must preserve.  When a
    value has a recognized semantic kind, all answer values of that kind must
    be among the required values; this rejects an invented value such as
    ``99 days`` even when the answer also repeats the supported ``17 days``.
    ``must_be_unknown`` expresses a partial-evidence answer without requiring
    one exact sentence.
    """

    claim_id: str
    subject_terms: tuple[str, ...] = ()
    required_values: tuple[str, ...] = ()
    forbidden_values: tuple[str, ...] = ()
    value_kinds: tuple[str, ...] = ()
    must_be_unknown: bool = False
    unknown_markers: tuple[str, ...] = _UNKNOWN_MARKERS
    allowed_answer_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroundingSpec:
    """Complete, reusable grounding contract for one fixture."""

    claims: tuple[GroundingClaim, ...]
    score: float = 0.98
    reject_unlisted_typed_values: bool = True


@dataclass(frozen=True, slots=True)
class _SemanticValue:
    kind: str
    value: str


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("’", "'").replace("-", " ")
    return " ".join(_TOKEN.findall(value))


def _number(value: str) -> str | None:
    normalized = value.casefold().replace(",", ".")
    if normalized in _NUMBER_WORDS:
        return str(_NUMBER_WORDS[normalized])
    parts = normalized.replace("-", " ").split()
    if len(parts) == 2 and parts[0] in {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"} and parts[1] in _NUMBER_WORDS:
        return str(_NUMBER_WORDS[parts[0]] + _NUMBER_WORDS[parts[1]])
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    if parsed == parsed.to_integral():
        return str(parsed.quantize(Decimal("1")))
    return format(parsed.normalize(), "f")


def _semantic_values(text: str) -> tuple[_SemanticValue, ...]:
    """Extract only bounded, typed values; prose itself is never treated as a claim."""

    found: list[_SemanticValue] = []
    occupied: list[tuple[int, int]] = []

    for match in _DATE.finditer(text):
        year, month, day = int(match["year"]), int(match["month"]), int(match["day"])
        try:
            date(year, month, day)
        except ValueError:
            continue
        value = f"{year:04d}-{month:02d}-{day:02d}"
        found.append(_SemanticValue("date", value))
        occupied.append(match.span())

    for pattern in (_DATE_DMY, _DATE_MDY):
        for match in pattern.finditer(text):
            if any(start < match.end() and match.start() < end for start, end in occupied):
                continue
            year = int(match["year"])
            month = _MONTHS[match.group("month").casefold()]
            day = int(match["day"])
            try:
                date(year, month, day)
            except ValueError:
                continue
            value = f"{year:04d}-{month:02d}-{day:02d}"
            found.append(_SemanticValue("date", value))
            occupied.append(match.span())

    for match in _MONEY.finditer(text):
        raw = match.group("prefix") or match.group("suffix")
        normalized = _number(raw)
        if normalized is None:
            continue
        found.append(_SemanticValue("money", normalized))
        occupied.append(match.span())

    for match in _QUANTITY.finditer(text):
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        number = _number(match.group("number"))
        unit = _UNIT_ALIASES.get(match.group("unit").casefold())
        if number is not None and unit is not None:
            found.append(_SemanticValue("quantity", f"{number} {unit}"))

    return tuple(found)


def _constraint_values(values: Sequence[str]) -> tuple[_SemanticValue, ...]:
    extracted: list[_SemanticValue] = []
    for value in values:
        extracted.extend(_semantic_values(value))
    return tuple(extracted)


def _has_term(answer: str, terms: Sequence[str]) -> bool:
    answer_tokens = _normalize(answer).split()
    for term in terms:
        term_tokens = _normalize(term).split()
        if not term_tokens:
            continue
        width = len(term_tokens)
        if any(answer_tokens[index:index + width] == term_tokens for index in range(len(answer_tokens) - width + 1)):
            return True
    return False


def _required_value_is_negated(answer: str, required: _SemanticValue) -> bool:
    """Conservatively reject a required value inside a negated clause."""

    for clause in re.split(r"[.;!?,]|\b(?:but|however|although|yet)\b", answer, flags=re.IGNORECASE):
        if required not in _semantic_values(clause):
            continue
        token_list = _normalize(clause).split()
        tokens = set(token_list)
        contraction_cannot = any(
            token_list[index:index + 2] == ["can", "t"]
            for index in range(len(token_list) - 1)
        )
        if tokens & _NEGATION_TERMS or contraction_cannot:
            return True
    return False


def _contains_forbidden_directive(answer: str, directive: str) -> bool:
    """Match a declared directive even when harmless stopwords are omitted."""

    answer_tokens = {
        _DIRECTIVE_TOKEN_ALIASES.get(token, token)
        for token in _normalize(answer).split()
    }
    directive_tokens = {
        _DIRECTIVE_TOKEN_ALIASES.get(token, token)
        for token in _normalize(directive).split()
        if token not in _FORBIDDEN_MATCH_STOPWORDS
    }
    return bool(directive_tokens) and directive_tokens.issubset(answer_tokens)


def evaluate_grounding(
    answer: str,
    spec: GroundingSpec,
    citation_ids: Sequence[str],
    available_citation_ids: Sequence[str],
    evidence_by_citation_id: Mapping[str, str],
) -> Mapping[str, object]:
    """Return the strict validator shape used by ``validate_grounding_result``."""

    if not isinstance(answer, str) or not answer.strip() or not spec.claims:
        return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

    cited = tuple(citation_ids)
    available = tuple(available_citation_ids)
    if (
        not cited
        or len(cited) != len(set(cited))
        or len(available) != len(set(available))
        or any(item not in available for item in cited)
        or any(item not in evidence_by_citation_id for item in cited)
        or any(not isinstance(evidence_by_citation_id[item], str) for item in cited)
    ):
        return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

    answer_values = _semantic_values(answer)
    answer_value_set = set(answer_values)
    normalized_answer = _normalize(answer)
    cited_evidence = "\n".join(evidence_by_citation_id[item] for item in cited)
    cited_evidence_values = set(_semantic_values(cited_evidence))
    cited_evidence_tokens = set(_normalize(cited_evidence).split())

    required_semantic: set[_SemanticValue] = set()
    constrained_kinds: set[str] = set()
    allowed_answer_tokens = set(_SAFE_LEXICAL_TOKENS) | cited_evidence_tokens
    allowed_numeric_tokens: set[str] = set()
    for claim in spec.claims:
        required = _constraint_values(claim.required_values)
        required_semantic.update(required)
        constrained_kinds.update(claim.value_kinds)
        constrained_kinds.update(item.kind for item in required)
        for semantic_value in required:
            if semantic_value not in cited_evidence_values:
                return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
            for token in re.findall(r"\d+(?:[.]\d+)?", semantic_value.value):
                normalized_number = _number(token)
                if normalized_number is not None:
                    allowed_numeric_tokens.add(normalized_number)

        if claim.subject_terms and not _has_term(answer, claim.subject_terms):
            return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
        if claim.subject_terms and not _has_term(cited_evidence, claim.subject_terms):
            return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
        for term in (*claim.subject_terms, *claim.unknown_markers, *claim.allowed_answer_terms):
            allowed_answer_tokens.update(_normalize(term).split())

        for raw_value in claim.required_values:
            semantic_value = _semantic_values(raw_value)
            if semantic_value:
                supported = any(value in answer_value_set for value in semantic_value)
            else:
                supported = _has_term(normalized_answer, (raw_value,))
                if not _has_term(cited_evidence, (raw_value,)):
                    return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
            if not supported:
                return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
            if semantic_value and any(
                _required_value_is_negated(answer, value) for value in semantic_value
            ):
                return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

        if claim.must_be_unknown:
            if not _has_term(answer, claim.unknown_markers):
                return {"grounded": False, "score": 0.0, "matchedCitationIds": []}
            if not _has_term(cited_evidence, _EVIDENCE_ABSENCE_MARKERS):
                return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

        forbidden = _constraint_values(claim.forbidden_values)
        if any(value in answer_value_set for value in forbidden) or any(
            _contains_forbidden_directive(normalized_answer, value)
            for value in claim.forbidden_values
        ):
            return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

    allowed_by_kind: dict[str, set[str]] = {}
    for value in required_semantic:
        allowed_by_kind.setdefault(value.kind, set()).add(value.value)
    if spec.reject_unlisted_typed_values:
        constrained_kinds.update(_TYPED_VALUE_KINDS)
    for value in answer_values:
        if value.kind in constrained_kinds and value.value not in allowed_by_kind.get(value.kind, set()):
            return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

    for token in _normalize(answer).split():
        normalized_number = _number(token)
        ordinal = re.fullmatch(r"(?P<number>\d+)(?:st|nd|rd|th)", token)
        if normalized_number is None and ordinal is not None:
            normalized_number = _number(ordinal.group("number"))
        if token in allowed_answer_tokens:
            continue
        if normalized_number is not None and normalized_number in allowed_numeric_tokens:
            continue
        return {"grounded": False, "score": 0.0, "matchedCitationIds": []}

    return {"grounded": True, "score": float(spec.score), "matchedCitationIds": list(cited)}


__all__ = ["GroundingClaim", "GroundingSpec", "evaluate_grounding"]
