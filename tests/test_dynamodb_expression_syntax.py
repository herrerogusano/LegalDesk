"""Guard DynamoDB expressions against grammar DynamoDB rejects at runtime.

Fakes and in-memory repositories do not parse ConditionExpression strings, so
an invalid expression only fails in AWS.  DynamoDB has no ``NOT IN`` operator;
negated membership must be written ``NOT (#attr IN (...))``.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "backend" / "src"
EXPRESSION_KEYWORDS = {"ConditionExpression", "FilterExpression", "KeyConditionExpression", "UpdateExpression"}
NOT_IN = re.compile(r"\bNOT\s+IN\b", re.IGNORECASE)


def _expression_literals(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.keyword) and node.arg in EXPRESSION_KEYWORDS:
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    found.append((inner.lineno, inner.value))
    return found


class DynamoDBExpressionSyntaxTests(unittest.TestCase):
    def test_no_expression_uses_not_in_operator(self) -> None:
        offenders = [
            f"{path.relative_to(SOURCE_ROOT)}:{line}"
            for path in SOURCE_ROOT.rglob("*.py")
            for line, text in _expression_literals(path)
            if NOT_IN.search(text)
        ]
        self.assertEqual(offenders, [], "DynamoDB rejects NOT IN; use NOT (#attr IN (...))")

    def test_guard_sees_the_attempt_exhaustion_condition(self) -> None:
        literals = [text for _, text in _expression_literals(SOURCE_ROOT / "legaldesk" / "idp" / "persistence.py")]
        self.assertTrue(any("NOT (#status IN (" in text and ":max" in text for text in literals))


if __name__ == "__main__":
    unittest.main()
