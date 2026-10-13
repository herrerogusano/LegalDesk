"""Per-container cache for boto3 clients and DynamoDB tables.

Only immutable infrastructure handles live here (clients, table proxies and
the session that builds them).  Nothing scoped to a request, token, grant,
actor or matter may be stored.  Building a boto3 resource loads and parses the
service models, which costs several seconds on a small Lambda; doing it once
per container (ideally during the boosted-CPU init phase via ``prewarm``)
removes that cost from the first request.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

_LOCK = threading.RLock()
_TABLES: dict[tuple[object, str], Any] = {}
_CLIENTS: dict[tuple[object, ...], Any] = {}
_LOGGER = logging.getLogger(__name__)


def _boto3() -> Any:
    import boto3

    return boto3


def _factory_identity() -> object:
    # Tests that monkeypatch boto3.resource/client must not see stale entries.
    boto3 = _boto3()
    return (id(getattr(boto3, "resource", None)), id(getattr(boto3, "client", None)))


def dynamodb_table(table_name: str) -> Any:
    """Return a cached DynamoDB Table proxy for the current container."""

    key = (_factory_identity(), table_name)
    with _LOCK:
        table = _TABLES.get(key)
        if table is None:
            table = _boto3().resource("dynamodb").Table(table_name)
            _TABLES[key] = table
        return table


def s3_client(*, region_name: str | None = None, config_factory: Callable[[], Any] | None = None) -> Any:
    """Return a cached S3 client (optionally with a botocore Config)."""

    key = (_factory_identity(), "s3", region_name, bool(config_factory))
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            kwargs: dict[str, Any] = {}
            if region_name:
                kwargs["region_name"] = region_name
            if config_factory is not None:
                kwargs["config"] = config_factory()
            client = _boto3().client("s3", **kwargs)
            _CLIENTS[key] = client
        return client


def in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME")) and not os.environ.get("LEGALDESK_DISABLE_PREWARM")


def prewarm(*, tables: tuple[str | None, ...] = (), s3: bool = False) -> None:
    """Best-effort init-phase warm-up; failures are swallowed (lazy path remains)."""

    if not in_lambda():
        return
    try:
        for name in tables:
            if name:
                dynamodb_table(name)
        if s3:
            region = os.environ.get("AWS_REGION")
            s3_client(region_name=region, config_factory=_s3_config)
    except Exception:  # pragma: no cover - warm-up must never break init
        _LOGGER.warning("prewarm skipped")


def _s3_config() -> Any:
    from botocore.config import Config

    return Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1, "mode": "standard"})


def default_s3_config() -> Any:
    return _s3_config()


def clear() -> None:
    with _LOCK:
        _TABLES.clear()
        _CLIENTS.clear()
