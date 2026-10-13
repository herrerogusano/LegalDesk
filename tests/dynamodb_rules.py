"""Server-side DynamoDB request rules that in-memory fakes tend to skip.

DynamoDB rejects a request when ``ExpressionAttributeValues`` /
``ExpressionAttributeNames`` contain an alias that no expression references
("unused in expressions"), or when an expression references an alias that was
not supplied.  boto3 also returns every stored number as ``Decimal``.  Fakes
call :func:`enforce_expression_rules` per request and :func:`as_boto3_item` on
reads so these two failure classes surface in unit tests instead of in AWS.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

_EXPRESSION_KEYS = ("UpdateExpression", "ConditionExpression", "FilterExpression", "KeyConditionExpression", "ProjectionExpression")
_VALUE_ALIAS = re.compile(r":[A-Za-z_][A-Za-z0-9_]*")
_NAME_ALIAS = re.compile(r"#[A-Za-z_][A-Za-z0-9_]*")


class DynamoValidationError(Exception):
    """Mirrors the ValidationException DynamoDB raises for malformed requests."""


def enforce_expression_rules(request: Mapping[str, Any]) -> None:
    expressions = [request[key] for key in _EXPRESSION_KEYS if isinstance(request.get(key), str)]
    text = " ".join(expressions)
    used_values = set(_VALUE_ALIAS.findall(text))
    used_names = set(_NAME_ALIAS.findall(text))
    given_values = set((request.get("ExpressionAttributeValues") or {}).keys())
    given_names = set((request.get("ExpressionAttributeNames") or {}).keys())
    if given_values - used_values:
        raise DynamoValidationError(f"ExpressionAttributeValues unused in expressions: {sorted(given_values - used_values)}")
    if used_values - given_values:
        raise DynamoValidationError(f"ExpressionAttributeValues missing for: {sorted(used_values - given_values)}")
    if given_names - used_names:
        raise DynamoValidationError(f"ExpressionAttributeNames unused in expressions: {sorted(given_names - used_names)}")
    if used_names - given_names:
        raise DynamoValidationError(f"ExpressionAttributeNames missing for: {sorted(used_names - given_names)}")


def as_boto3_item(value: Any) -> Any:
    """Deep copy with ints/floats turned into Decimal, as boto3 returns them."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: as_boto3_item(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_boto3_item(item) for item in value]
    return value
