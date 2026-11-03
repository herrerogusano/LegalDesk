"""Synthetic state that a live check or eval leaves in the metadata table (ids only, never content).

Shared by ``scripts/phase15_harness_checks.py`` and ``evals/phase15_routing_eval.py`` so both reports carry the same
``residualState`` block and one cleanup (``docs/phase-15-live-runbook.md``, step 14) reads either of them.

Each agent call builds the worker's sealed binding, which writes:

* a **conversation binding** ``CONVERSATION#<tenant>#<matter>#<user>#<conversation>`` / ``SESSION#<selector>``:
  no TTL, and nothing in the project sweeps it, so it must be deleted by hand;
* an **invocation grant** with ``ttl`` = 5 minutes (DynamoDB TTL and the reconciliation sweep); the interceptor adds
  one equally self-expiring grant per tool call.

Nothing else is written: the job is only in memory, and no quota counter, Harness or backend memory event, citation
handle or CHATJOB item is touched.

The report stores random ids and the SHA-256 of the partition key, never the key, because the key holds the tenant,
the matter and the user.
"""

from __future__ import annotations

import hashlib

GRANT_TTL_SECONDS = 300  # gateway_interceptor.GATEWAY_INVOCATION_TTL_SECONDS
CLEANUP_POINTER = "delete the conversation bindings (no TTL); grants expire by themselves (docs/phase-15-live-runbook.md step 14)"


def key_sha256(partition_key: str) -> str:
    return hashlib.sha256(partition_key.encode("utf-8")).hexdigest()


class ResidualState:
    """Bindings and grants written so far; filled *before* the call that could fail."""

    def __init__(self) -> None:
        self.bindings: list[dict[str, object]] = []

    def conversation_bound(self, conversation_id: str, session_selector: str, partition_key: str) -> dict[str, object]:
        entry: dict[str, object] = {
            "conversationId": conversation_id, "sessionSelector": session_selector,
            "bindingPkSha256": key_sha256(partition_key), "invocationId": None,
        }
        self.bindings.append(entry)  # recorded before the call, so a failure after the bind still leaves the ids
        return entry

    def as_report(self) -> dict[str, object]:
        return {
            "conversationBindings": [dict(item) for item in self.bindings],
            "invocationGrantsWritten": sum(1 for item in self.bindings if item["invocationId"]),
            "grantTtlSeconds": GRANT_TTL_SECONDS,
            "cleanup": CLEANUP_POINTER,
        }
