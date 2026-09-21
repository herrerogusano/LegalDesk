"""Local-only request and token budget guards for the Phase 13 smoke.

This module does not create clients, call AWS, or implement billing.  It wraps
already-created SDK clients and reserves a bounded operation before dispatch.
An exception still consumes the operation slot.  Numeric Harness usage is
recorded separately after the adapter has validated the provider stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Callable, Mapping


class SmokeBudgetExceeded(RuntimeError):
    """A smoke operation or observed token total exceeded its hard ceiling."""


class SmokeUsageInvalid(ValueError):
    """Provider usage was not a finite, non-negative integer count."""


@dataclass(frozen=True, slots=True)
class SmokeBudgetLimits:
    converse: int = 4
    retrieve: int = 2
    apply_guardrail: int = 8
    invoke_harness: int = 2
    create_event: int = 20
    list_events: int = 20
    start_ingestion_job: int = 1
    get_ingestion_job: int = 20
    s3: int = 30
    dynamodb: int = 400
    input_tokens: int = 160_000
    output_tokens: int = 5_120


@dataclass(frozen=True, slots=True)
class SmokeBudgetSnapshot:
    counts: Mapping[str, int]
    input_tokens: int
    output_tokens: int


@dataclass(slots=True)
class _Reservation:
    budget: "SmokeBudget"
    operation: str
    estimated_input: int
    estimated_output: int
    settled: bool = False

    def observe(self, *, input_tokens: object = 0, output_tokens: object = 0) -> None:
        if self.settled:
            raise SmokeBudgetExceeded("budget reservation already settled")
        try:
            self.budget._settle(self, input_tokens, output_tokens)
        except SmokeUsageInvalid:
            self.budget._release_estimate(self)
            self.settled = True
            raise
        except SmokeBudgetExceeded:
            # _settle has already consumed the reservation and latched the
            # budget when a numeric ceiling is exceeded.
            self.settled = True
            raise
        self.settled = True

    def complete(self) -> None:
        """Settle with the conservative pre-call estimate."""

        self.observe(input_tokens=self.estimated_input, output_tokens=self.estimated_output)

    def __enter__(self) -> "_Reservation":
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        if not self.settled:
            self.budget._release_estimate(self)
        return False


@dataclass(slots=True)
class SmokeBudget:
    """Process-local hard budget shared by all explicitly wrapped clients."""

    limits: SmokeBudgetLimits = field(default_factory=SmokeBudgetLimits)
    _counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _reserved_input: int = field(default=0, init=False, repr=False)
    _reserved_output: int = field(default=0, init=False, repr=False)
    _input_tokens: int = field(default=0, init=False, repr=False)
    _output_tokens: int = field(default=0, init=False, repr=False)
    _halted: bool = field(default=False, init=False, repr=False)
    _halt_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        for operation in _OPERATION_LIMITS:
            self._counts[operation] = 0

    def reserve(
        self,
        operation: str,
        *,
        estimated_input_tokens: object = 0,
        estimated_output_tokens: object = 0,
    ) -> _Reservation:
        self._ensure_open()
        limit = _limit_for(self.limits, operation)
        current = self._counts[operation]
        if current >= limit:
            self._halt(f"smoke budget exhausted: {operation}")
            raise SmokeBudgetExceeded(f"smoke budget exhausted: {operation}")
        estimated_input = _count(estimated_input_tokens, "estimated_input_tokens")
        estimated_output = _count(estimated_output_tokens, "estimated_output_tokens")
        if self._input_tokens + self._reserved_input + estimated_input > self.limits.input_tokens:
            raise SmokeBudgetExceeded("smoke input-token budget exhausted")
        if self._output_tokens + self._reserved_output + estimated_output > self.limits.output_tokens:
            raise SmokeBudgetExceeded("smoke output-token budget exhausted")
        self._counts[operation] = current + 1
        self._reserved_input += estimated_input
        self._reserved_output += estimated_output
        return _Reservation(self, operation, estimated_input, estimated_output)

    def call(self, operation: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Reserve, dispatch once, and count the operation even on failure."""

        with self.reserve(operation) as reservation:
            try:
                result = function(*args, **kwargs)
            except BaseException:
                raise
            reservation.complete()
            return result

    def consume_harness_usage(self, usage: object) -> None:
        """Consume validated numeric usage from ``InvokeResult.usage``.

        The Harness adapter performs the provider-shape validation.  This
        second boundary still validates the numeric values so callers cannot
        bypass the aggregate smoke ceiling with an arbitrary object.
        """

        self._ensure_open()
        if usage is None:
            self._halt("Harness usage was not observed")
            raise SmokeUsageInvalid("Harness usage is required for the smoke budget")
        try:
            input_tokens = _usage_value(usage, "input_tokens", "inputTokens")
            output_tokens = _usage_value(usage, "output_tokens", "outputTokens")
        except SmokeUsageInvalid as exc:
            self._halt(str(exc))
            raise
        if self._input_tokens + self._reserved_input + input_tokens > self.limits.input_tokens:
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._halt("smoke input-token budget exhausted")
            raise SmokeBudgetExceeded("smoke input-token budget exhausted")
        if self._output_tokens + self._reserved_output + output_tokens > self.limits.output_tokens:
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._halt("smoke output-token budget exhausted")
            raise SmokeBudgetExceeded("smoke output-token budget exhausted")
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens

    def consume_converse_usage(self, usage: object) -> None:
        """Consume one provider-shaped Converse usage record."""

        self.consume_harness_usage(usage)

    def snapshot(self) -> SmokeBudgetSnapshot:
        return SmokeBudgetSnapshot(dict(self._counts), self._input_tokens, self._output_tokens)

    def _settle(self, reservation: _Reservation, input_tokens: object, output_tokens: object) -> None:
        try:
            actual_input = _count(input_tokens, "input_tokens")
            actual_output = _count(output_tokens, "output_tokens")
        except SmokeUsageInvalid as exc:
            self._halt(str(exc))
            raise
        other_reserved_input = self._reserved_input - reservation.estimated_input
        other_reserved_output = self._reserved_output - reservation.estimated_output
        if self._input_tokens + actual_input + other_reserved_input > self.limits.input_tokens:
            self._reserved_input -= reservation.estimated_input
            self._reserved_output -= reservation.estimated_output
            self._input_tokens += actual_input
            self._output_tokens += actual_output
            self._halt("smoke input-token budget exhausted")
            raise SmokeBudgetExceeded("smoke input-token budget exhausted")
        if self._output_tokens + actual_output + other_reserved_output > self.limits.output_tokens:
            self._reserved_input -= reservation.estimated_input
            self._reserved_output -= reservation.estimated_output
            self._input_tokens += actual_input
            self._output_tokens += actual_output
            self._halt("smoke output-token budget exhausted")
            raise SmokeBudgetExceeded("smoke output-token budget exhausted")
        self._reserved_input -= reservation.estimated_input
        self._reserved_output -= reservation.estimated_output
        self._input_tokens += actual_input
        self._output_tokens += actual_output

    def _release_estimate(self, reservation: _Reservation) -> None:
        # A failed paid call has unknown provider usage. Keep its conservative
        # estimate and halt further paid work instead of treating it as free.
        self._reserved_input -= reservation.estimated_input
        self._reserved_output -= reservation.estimated_output
        self._input_tokens += reservation.estimated_input
        self._output_tokens += reservation.estimated_output
        self._halt("failed operation usage was not observed")

    def _halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def _ensure_open(self) -> None:
        if self._halted:
            raise SmokeBudgetExceeded(self._halt_reason or "smoke budget halted")


_METHOD_OPERATIONS = {
    "converse": "converse",
    "retrieve": "retrieve",
    "apply_guardrail": "apply_guardrail",
    "invoke_harness": "invoke_harness",
    "create_event": "create_event",
    "list_events": "list_events",
    "start_ingestion_job": "start_ingestion_job",
    "get_ingestion_job": "get_ingestion_job",
    "put_object": "s3",
    "get_object": "s3",
    "head_object": "s3",
    "delete_object": "s3",
    "copy_object": "s3",
    "generate_presigned_url": "s3",
    "get_item": "dynamodb",
    "query": "dynamodb",
    "put_item": "dynamodb",
    "update_item": "dynamodb",
    "delete_item": "dynamodb",
    "batch_get_item": "dynamodb",
    "batch_write_item": "dynamodb",
}
_OPERATION_LIMITS = frozenset(_METHOD_OPERATIONS.values())


class BudgetedSdkClient:
    """Transparent method proxy for the allowlisted SDK operations."""

    def __init__(self, client: object, budget: SmokeBudget) -> None:
        self._client = client
        self._budget = budget

    def __getattr__(self, name: str) -> Any:
        operation = _METHOD_OPERATIONS.get(name)
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute
        if operation is None:
            raise SmokeBudgetExceeded(f"unbudgeted SDK method: {name}")

        def invoke(*args: Any, **kwargs: Any) -> Any:
            result = self._budget.call(operation, attribute, *args, **kwargs)
            if operation == "converse":
                usage = result.get("usage") if isinstance(result, Mapping) else None
                self._budget.consume_converse_usage(usage)
            return result

        return invoke


def _limit_for(limits: SmokeBudgetLimits, operation: str) -> int:
    if operation not in _OPERATION_LIMITS:
        raise SmokeBudgetExceeded(f"unbudgeted smoke operation: {operation}")
    limit = getattr(limits, operation)
    if type(limit) is not int or limit < 0:
        raise ValueError(f"invalid limit for {operation}")
    return limit


def _count(value: object, name: str) -> int:
    if type(value) is not int:
        # Explicitly reject floats, NaN and infinity; the SDK shape is integer.
        if isinstance(value, float) and not isfinite(value):
            raise SmokeUsageInvalid(f"{name} must be finite")
        raise SmokeUsageInvalid(f"{name} must be an integer")
    if value < 0 or value > 10**12:
        raise SmokeUsageInvalid(f"{name} is out of bounds")
    return value


def _usage_value(usage: object, attribute: str, key: str) -> int:
    if isinstance(usage, Mapping):
        value = usage.get(key)
    else:
        value = getattr(usage, attribute, None)
    return _count(value, attribute)


__all__ = [
    "BudgetedSdkClient",
    "SmokeBudget",
    "SmokeBudgetExceeded",
    "SmokeBudgetLimits",
    "SmokeBudgetSnapshot",
    "SmokeUsageInvalid",
]
