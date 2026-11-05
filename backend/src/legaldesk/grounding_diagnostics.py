"""Closed diagnostic projections. Provider content never crosses this boundary."""
from __future__ import annotations

import re
from typing import Mapping

# Deliberately finite; unknown/new provider types collapse to OTHER.
POLICY_TYPES = {
    "sensitiveInformationPolicy": {"NAME", "ADDRESS", "EMAIL", "PHONE", "PASSWORD", "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "CREDIT_DEBIT_CARD_NUMBER", "US_SOCIAL_SECURITY_NUMBER", "OTHER", "REGEX"},
    "topicPolicy": {"DENY", "OTHER"},
    "contentPolicy": {"HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT", "PROMPT_ATTACK", "OTHER"},
    "wordPolicy": {"CUSTOM", "PROFANITY", "OTHER"},
    "contextualGroundingPolicy": {"GROUNDING", "RELEVANCE", "OTHER"},
    "automatedReasoningPolicy": {"OTHER"},
}
INTERVENTIONS = frozenset(
    f"{policy}:{kind}:{action}"
    for policy, kinds in POLICY_TYPES.items() for kind in kinds
    for action in ("ANONYMIZED", "BLOCKED", "BLOCK", "GUARDRAIL_INTERVENED")
)
LANGUAGES = {"en", "es", "mixed", "unknown"}


def language_hint(text: str) -> str:
    """Conservative stopword heuristic, not a language detector or a security gate."""
    words = set(re.findall(r"[^\W\d_]+", text.lower()))
    en = len(words & {"the", "and", "shall", "must", "is", "of", "with", "from"}) >= 2
    es = len(words & {"el", "la", "los", "las", "de", "del", "que", "con", "para", "por"}) >= 2
    return "mixed" if en and es else "en" if en else "es" if es else "unknown"


def grounding_diagnostics(response: object, question: str, answer: str, texts: list[str]) -> dict[str, object]:
    labels: set[str] = set()
    if isinstance(response, Mapping):
        assessments = response.get("assessments", ())
        for assessment in assessments if isinstance(assessments, (list, tuple)) else ():
            if not isinstance(assessment, Mapping):
                continue
            for policy, kinds in POLICY_TYPES.items():
                node = assessment.get(policy)
                if not isinstance(node, Mapping):
                    continue
                for group, entries in node.items():
                    if group not in {"piiEntities", "regexes", "topics", "filters", "customWords", "managedWordLists", "findings"} or not isinstance(entries, (list, tuple)):
                        continue
                    for item in entries:
                        if not isinstance(item, Mapping):
                            continue
                        action = item.get("action")
                        if not isinstance(action, str) or action not in {"ANONYMIZED", "BLOCKED", "BLOCK", "GUARDRAIL_INTERVENED"}:
                            continue
                        kind = "REGEX" if group == "regexes" else "CUSTOM" if group == "customWords" else item.get("type")
                        kind = kind if isinstance(kind, str) and kind in kinds else "OTHER"
                        labels.add(f"{policy}:{kind}:{action}")
    return {
        "topAction": response.get("action") if isinstance(response, Mapping) and isinstance(response.get("action"), str) and response.get("action") in {"NONE", "GUARDRAIL_INTERVENED"} else "OTHER",
        "policyInterventions": sorted(labels),
        "queryChars": len(question), "answerChars": len(answer),
        "sourceChars": sum(map(len, texts)), "sourceCount": len(texts),
        "sourceMaxChars": max(map(len, texts), default=0),
        "queryLanguageHint": language_hint(question), "answerLanguageHint": language_hint(answer),
        "sourceLanguageHint": language_hint(" ".join(texts)),
    }


def valid_grounding_diagnostics(value: object) -> bool:
    expected = {"topAction", "policyInterventions", "queryChars", "answerChars", "sourceChars",
                "sourceCount", "sourceMaxChars", "queryLanguageHint", "answerLanguageHint", "sourceLanguageHint"}
    if not isinstance(value, dict) or set(value) != expected:
        return False
    for key, item in value.items():
        if key == "policyInterventions":
            if not isinstance(item, list) or len(item) > len(INTERVENTIONS):
                return False
            if any(not isinstance(label, str) or label not in INTERVENTIONS for label in item):
                return False
        elif key == "topAction":
            if not isinstance(item, str) or item not in {"NONE", "GUARDRAIL_INTERVENED", "OTHER"}:
                return False
        elif key.endswith("LanguageHint"):
            if not isinstance(item, str) or item not in LANGUAGES:
                return False
        elif type(item) is not int or not 0 <= item <= 1_000_000:
            return False
    return True
