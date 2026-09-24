"""Server-side monthly beta quota ledger.

The public beta uses one counter item per tenant and UTC month in the existing
metadata table.  Reservations happen before a provider call and are strictly
fail-closed: a conditional failure is exposed as a quota exhaustion response,
and any other persistence error is also denied before the provider is reached.
"""

from __future__ import annotations

import calendar
import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol


UPLOADS = "uploads"
UPLOAD_BYTES = "upload_bytes"
CHATS = "chats"
INGESTION_STARTS = "ingestion_starts"
HARNESS = "harness"
GATEWAY = "gateway"
_OPERATIONS = frozenset({CHATS, INGESTION_STARTS, HARNESS, GATEWAY})
_COUNTER_FIELDS = {
    CHATS: "chatCount",
    INGESTION_STARTS: "ingestionStartCount",
    HARNESS: "harnessCount",
    GATEWAY: "gatewayCount",
}


class QuotaExceededError(Exception):
    """The configured monthly ceiling has been reached."""


class QuotaUnavailableError(Exception):
    """The quota authority could not safely reserve capacity."""


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    uploads_per_month: int = 50
    upload_bytes_per_month: int = 500 * 1024 * 1024
    chats_per_month: int = 300
    ingestion_starts_per_month: int = 50
    harness_per_month: int = 100
    gateway_per_month: int = 500

    def __post_init__(self) -> None:
        values = (
            self.uploads_per_month,
            self.upload_bytes_per_month,
            self.chats_per_month,
            self.ingestion_starts_per_month,
            self.harness_per_month,
            self.gateway_per_month,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("all monthly quota limits must be positive integers")
        # Prevent an accidental environment typo from becoming an unbounded
        # cost control.  The byte limit is intentionally larger than the
        # upload validation ceiling so it remains configurable for a beta.
        if self.uploads_per_month > 1_000_000 or self.upload_bytes_per_month > 1_000_000_000_000:
            raise ValueError("monthly quota limits are too large")
        if any(value > 10_000_000 for value in (self.chats_per_month, self.ingestion_starts_per_month, self.harness_per_month, self.gateway_per_month)):
            raise ValueError("monthly quota limits are too large")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "QuotaLimits":
        defaults = cls()

        def positive(name: str, default: int) -> int:
            raw = environ.get(name, str(default))
            if not isinstance(raw, str) or not raw.strip() or raw.strip() != raw or not raw.isascii() or not raw.isdecimal():
                raise ValueError(f"{name} must be a positive decimal integer")
            value = int(raw)
            if value <= 0:
                raise ValueError(f"{name} must be a positive decimal integer")
            return value

        return cls(
            uploads_per_month=positive("LEGALDESK_QUOTA_UPLOADS_PER_MONTH", defaults.uploads_per_month),
            upload_bytes_per_month=positive("LEGALDESK_QUOTA_UPLOAD_BYTES_PER_MONTH", defaults.upload_bytes_per_month),
            chats_per_month=positive("LEGALDESK_QUOTA_CHATS_PER_MONTH", defaults.chats_per_month),
            ingestion_starts_per_month=positive("LEGALDESK_QUOTA_INGESTION_STARTS_PER_MONTH", defaults.ingestion_starts_per_month),
            harness_per_month=positive("LEGALDESK_QUOTA_HARNESS_PER_MONTH", defaults.harness_per_month),
            gateway_per_month=positive("LEGALDESK_QUOTA_GATEWAY_PER_MONTH", defaults.gateway_per_month),
        )


def utc_month(now: float | int | None = None) -> str:
    """Return the billing month as an unambiguous UTC ``YYYY-MM`` value."""

    timestamp = time.time() if now is None else now
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError("quota clock must be numeric")
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m")


def scoped_idempotency_key(*, subject: str, tenant_id: str, matter_id: str, key: str) -> str:
    """Bind a client retry key to the server-derived request scope."""

    values = (subject, tenant_id, matter_id, key)
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
        raise ValueError("quota idempotency scope is invalid")
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _month_ttl(month: str) -> int:
    year, month_number = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, month_number)[1]
    end = datetime(year, month_number, days, 23, 59, 59, tzinfo=timezone.utc)
    # TTL is cleanup only; application counters are selected by the current
    # UTC month and never rely on DynamoDB TTL for authorization.
    return int((end + timedelta(days=31)).timestamp())


class QuotaLedger(Protocol):
    limits: QuotaLimits

    def reserve(
        self,
        tenant_id: str,
        operation: str,
        *,
        amount: int = 1,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> None: ...

    def reserve_upload(
        self, tenant_id: str, file_size_bytes: int, *, idempotency_key: str | None = None, now: float | None = None
    ) -> None: ...


def _validate_reservation(tenant_id: str, operation: str, amount: int) -> None:
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 128:
        raise ValueError("tenant_id is invalid")
    if operation not in _OPERATIONS:
        raise ValueError("quota operation is invalid")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("quota amount is invalid")


@dataclass(slots=True)
class DisabledQuotaLedger:
    """Explicit no-charge ledger used by loopback/non-public compositions."""

    limits: QuotaLimits = field(default_factory=QuotaLimits)

    def reserve(self, tenant_id: str, operation: str, *, amount: int = 1, idempotency_key: str | None = None, now: float | None = None) -> None:
        return None

    def reserve_upload(self, tenant_id: str, file_size_bytes: int, *, idempotency_key: str | None = None, now: float | None = None) -> None:
        return None


@dataclass(slots=True)
class InMemoryQuotaLedger:
    """Thread-safe deterministic ledger for tests and local development."""

    limits: QuotaLimits = field(default_factory=QuotaLimits)
    clock: Callable[[], float] = time.time
    counters: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    reservations: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _reserve_locked(self, tenant_id: str, month: str, operation: str, amount: int, idempotency_key: str | None) -> None:
        reservation_key = (tenant_id, month, operation, idempotency_key) if idempotency_key is not None else None
        if reservation_key is not None and reservation_key in self.reservations:
            return
        counter = self.counters.setdefault((tenant_id, month), {field: 0 for field in ("uploads", "uploadBytes", "chatCount", "ingestionStartCount", "harnessCount", "gatewayCount")})
        if operation == UPLOAD_BYTES:
            if counter["uploads"] + 1 > self.limits.uploads_per_month or counter["uploadBytes"] + amount > self.limits.upload_bytes_per_month:
                raise QuotaExceededError("monthly quota exceeded")
            counter["uploads"] += 1
            counter["uploadBytes"] += amount
        else:
            field_name = _COUNTER_FIELDS[operation]
            limit = {
                CHATS: self.limits.chats_per_month,
                INGESTION_STARTS: self.limits.ingestion_starts_per_month,
                HARNESS: self.limits.harness_per_month,
                GATEWAY: self.limits.gateway_per_month,
            }[operation]
            if counter[field_name] + amount > limit:
                raise QuotaExceededError("monthly quota exceeded")
            counter[field_name] += amount
        if reservation_key is not None:
            self.reservations[reservation_key] = amount

    def reserve(self, tenant_id: str, operation: str, *, amount: int = 1, idempotency_key: str | None = None, now: float | None = None) -> None:
        _validate_reservation(tenant_id, operation, amount)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128):
            raise ValueError("quota idempotency key is invalid")
        month = utc_month(self.clock() if now is None else now)
        with self._lock:
            self._reserve_locked(tenant_id, month, operation, amount, idempotency_key)

    def reserve_upload(self, tenant_id: str, file_size_bytes: int, *, idempotency_key: str | None = None, now: float | None = None) -> None:
        if isinstance(file_size_bytes, bool) or not isinstance(file_size_bytes, int) or file_size_bytes <= 0:
            raise ValueError("upload size is invalid")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128):
            raise ValueError("quota idempotency key is invalid")
        month = utc_month(self.clock() if now is None else now)
        with self._lock:
            self._reserve_locked(tenant_id, month, UPLOAD_BYTES, file_size_bytes, idempotency_key)


class DynamoDBQuotaLedger:
    """DynamoDB-backed ledger using one conditional UpdateItem per reservation."""

    _PREFIX = "LEGALDESK#P14#QUOTA#TENANT#"

    def __init__(self, table_name: str, *, table: Any | None = None, limits: QuotaLimits | None = None, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        self.limits = limits or QuotaLimits()
        self.clock = clock
        if table is None:
            import boto3
            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @classmethod
    def _pk(cls, tenant_id: str, month: str) -> str:
        return f"{cls._PREFIX}{tenant_id}#MONTH#{month}"

    @staticmethod
    def _conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        code = response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
        return code == "ConditionalCheckFailedException" or "conditional check failed" in str(exc).lower()

    @staticmethod
    def _token(operation: str, key: str) -> str:
        return hashlib.sha256(f"{operation}\x1f{key}".encode("utf-8")).hexdigest()

    def _replay_or_exceeded(self, key: dict[str, str], token: str, exc: Exception) -> None:
        if not self._conditional_failure(exc):
            raise QuotaUnavailableError("quota reservation unavailable") from exc
        try:
            response = self.table.get_item(Key=key, ConsistentRead=True)
        except Exception as read_exc:
            raise QuotaUnavailableError("quota reservation unavailable") from read_exc
        item = response.get("Item") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping):
            raise QuotaUnavailableError("quota reservation unavailable")
        tokens = item.get("idempotencyTokens", ())
        if not isinstance(tokens, (set, frozenset, list, tuple)) or any(not isinstance(value, str) for value in tokens):
            raise QuotaUnavailableError("quota reservation unavailable")
        if token in tokens:
            return
        raise QuotaExceededError("monthly quota exceeded") from exc

    def reserve(self, tenant_id: str, operation: str, *, amount: int = 1, idempotency_key: str | None = None, now: float | None = None) -> None:
        _validate_reservation(tenant_id, operation, amount)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128):
            raise ValueError("quota idempotency key is invalid")
        month = utc_month(self.clock() if now is None else now)
        field_name = _COUNTER_FIELDS[operation]
        limit = {
            CHATS: self.limits.chats_per_month,
            INGESTION_STARTS: self.limits.ingestion_starts_per_month,
            HARNESS: self.limits.harness_per_month,
            GATEWAY: self.limits.gateway_per_month,
        }[operation]
        if amount > limit:
            raise QuotaExceededError("monthly quota exceeded")
        key = {"pk": self._pk(tenant_id, month), "sk": "COUNTER"}
        names = {"#entity": "entityType", "#tenant": "tenantId", "#month": "monthUtc", "#ttl": "ttl", "#counter": field_name}
        values: dict[str, object] = {":entity": "P14QuotaCounter", ":tenant": tenant_id, ":month": month, ":ttl": _month_ttl(month), ":amount": amount, ":remaining": limit - amount}
        condition = "attribute_not_exists(#counter) OR #counter <= :remaining"
        update = "SET #entity = if_not_exists(#entity, :entity), #tenant = if_not_exists(#tenant, :tenant), #month = if_not_exists(#month, :month), #ttl = if_not_exists(#ttl, :ttl) ADD #counter :amount"
        token = None
        if idempotency_key is not None:
            token = self._token(operation, idempotency_key)
            names["#tokens"] = "idempotencyTokens"
            values[":token"] = token
            values[":token_set"] = {token}
            condition = f"({condition}) AND (attribute_not_exists(#tokens) OR NOT contains(#tokens, :token))"
            update += " , #tokens :token_set"
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression=update,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
            )
        except Exception as exc:
            if token is not None:
                self._replay_or_exceeded(key, token, exc)
            elif self._conditional_failure(exc):
                raise QuotaExceededError("monthly quota exceeded") from exc
            else:
                raise QuotaUnavailableError("quota reservation unavailable") from exc

    def reserve_upload(self, tenant_id: str, file_size_bytes: int, *, idempotency_key: str | None = None, now: float | None = None) -> None:
        if isinstance(file_size_bytes, bool) or not isinstance(file_size_bytes, int) or file_size_bytes <= 0:
            raise ValueError("upload size is invalid")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128):
            raise ValueError("quota idempotency key is invalid")
        month = utc_month(self.clock() if now is None else now)
        if file_size_bytes > self.limits.upload_bytes_per_month:
            raise QuotaExceededError("monthly quota exceeded")
        key = {"pk": self._pk(tenant_id, month), "sk": "COUNTER"}
        names = {"#entity": "entityType", "#tenant": "tenantId", "#month": "monthUtc", "#ttl": "ttl", "#uploads": "uploads", "#bytes": "uploadBytes"}
        values: dict[str, object] = {":entity": "P14QuotaCounter", ":tenant": tenant_id, ":month": month, ":ttl": _month_ttl(month), ":one": 1, ":bytes": file_size_bytes, ":remaining_uploads": self.limits.uploads_per_month - 1, ":remaining_bytes": self.limits.upload_bytes_per_month - file_size_bytes}
        condition = "(attribute_not_exists(#uploads) OR #uploads <= :remaining_uploads) AND (attribute_not_exists(#bytes) OR #bytes <= :remaining_bytes)"
        update = "SET #entity = if_not_exists(#entity, :entity), #tenant = if_not_exists(#tenant, :tenant), #month = if_not_exists(#month, :month), #ttl = if_not_exists(#ttl, :ttl) ADD #uploads :one, #bytes :bytes"
        token = None
        if idempotency_key is not None:
            token = self._token(UPLOAD_BYTES, idempotency_key)
            names["#tokens"] = "idempotencyTokens"
            values[":token"] = token
            values[":token_set"] = {token}
            condition = f"({condition}) AND (attribute_not_exists(#tokens) OR NOT contains(#tokens, :token))"
            update += " , #tokens :token_set"
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression=update,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
            )
        except Exception as exc:
            if token is not None:
                self._replay_or_exceeded(key, token, exc)
            elif self._conditional_failure(exc):
                raise QuotaExceededError("monthly quota exceeded") from exc
            else:
                raise QuotaUnavailableError("quota reservation unavailable") from exc


__all__ = [
    "CHATS", "DynamoDBQuotaLedger", "DisabledQuotaLedger", "GATEWAY", "HARNESS",
    "INGESTION_STARTS", "InMemoryQuotaLedger", "QuotaExceededError", "QuotaLimits",
    "QuotaUnavailableError", "QuotaLedger", "UPLOADS", "scoped_idempotency_key", "utc_month",
]
