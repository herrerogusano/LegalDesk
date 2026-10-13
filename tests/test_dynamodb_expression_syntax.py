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

    def test_literal_attribute_values_are_all_referenced(self) -> None:
        """DynamoDB rejects ExpressionAttributeValues no expression uses."""

        alias = re.compile(r":[A-Za-z_][A-Za-z0-9_]*")
        offenders: list[str] = []
        checked = 0
        for path in SOURCE_ROOT.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                values = keywords.get("ExpressionAttributeValues")
                if not isinstance(values, ast.Dict) or any(key is None or not isinstance(key, ast.Constant) for key in values.keys):
                    continue
                expressions = [keywords[name] for name in EXPRESSION_KEYWORDS if name in keywords]
                if not expressions:
                    continue
                text = " ".join(inner.value for expression in expressions for inner in ast.walk(expression) if isinstance(inner, ast.Constant) and isinstance(inner.value, str))
                if any(isinstance(inner, (ast.Name, ast.JoinedStr, ast.BinOp, ast.Call)) for expression in expressions for inner in ast.walk(expression)):
                    continue  # expression built at runtime; the DynamoDB-rules fakes cover it
                checked += 1
                unused = sorted(str(key.value) for key in values.keys if str(key.value) not in set(alias.findall(text)))
                if unused:
                    offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} unused {unused}")
        self.assertEqual(offenders, [])
        self.assertGreater(checked, 5)

    def test_guard_sees_the_attempt_exhaustion_condition(self) -> None:
        literals = [text for _, text in _expression_literals(SOURCE_ROOT / "legaldesk" / "idp" / "persistence.py")]
        self.assertTrue(any("NOT (#status IN (" in text and ":max" in text for text in literals))


if __name__ == "__main__":
    unittest.main()
