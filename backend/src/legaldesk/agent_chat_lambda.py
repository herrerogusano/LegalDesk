"""Disabled-by-default SQS Lambda boundary of the asynchronous agentic chat (ADR-021 D4).

* One message per invocation (``BatchSize`` 1). The body is exactly
  ``{"jobId", "tenantId", "matterId"}``; it is server-authored, carries no
  question, token or session key, and is never an authorization: the worker
  re-derives identity and scope from the stored job and the server-side session.
* A transient error is retried at most once: the worker moves the job back to
  ``queued`` and this handler shortens the message visibility to 30 s
  (``ChangeMessageVisibility``) so the retry fits the 5-minute job deadline,
  then reports the message as a batch item failure. Anything the worker could
  not classify is treated the same way; the second failure goes to the DLQ
  (``maxReceiveCount`` 2).
* Composition is lazy and only runs when ``LEGALDESK_AGENT_CHAT_ENABLED`` is true.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_chat_telemetry import sanitize_job_metadata
from .agent_jobs import RETRY_VISIBILITY_SECONDS, JobKey

MAX_MESSAGE_BYTES = 1024
_MESSAGE_KEYS = frozenset({"jobId", "tenantId", "matterId"})
AGENT_PROMPT_FILENAME = "legaldesk-agent.md"


def default_agent_prompt_path(module_file: str | Path = __file__) -> Path:
    """Where the agent prompt lives next to this module, in the Lambda zip or in a checkout.

    In the zip the module is ``<root>/legaldesk/agent_chat_lambda.py`` and the prompt
    ``<root>/prompts/...`` (``parents[1]``); in a checkout it is
    ``<repo>/backend/src/legaldesk/...`` and ``<repo>/prompts/...`` (``parents[3]``).
    The deployed template also sets ``LEGALDESK_AGENT_PROMPT_PATH`` explicitly.
    """

    here = Path(module_file).resolve()
    candidates = [parent / "prompts" / AGENT_PROMPT_FILENAME for parent in (here.parents[1:2] + here.parents[3:4])]
    candidates.append(here.parent / "prompts" / AGENT_PROMPT_FILENAME)  # shallow layouts: never fail to build a path
    return next((path for path in candidates if path.is_file()), candidates[0])


class AgentChatLambdaConfigurationError(RuntimeError):
    pass


class Boto3AgentChatQueue:
    """Publishes job ids; the only thing that ever travels through the queue."""

    def __init__(self, client: Any, *, queue_url: str) -> None:
        if client is None or not isinstance(queue_url, str) or not queue_url.strip():
            raise AgentChatLambdaConfigurationError("agent chat queue configuration is invalid")
        self.client, self.queue_url = client, queue_url

    def publish(self, key: JobKey) -> None:
        body = json.dumps({"jobId": key.job_id, "tenantId": key.tenant_id, "matterId": key.matter_id}, separators=(",", ":"))
        response = self.client.send_message(QueueUrl=self.queue_url, MessageBody=body)
        if not isinstance(response, Mapping) or not isinstance(response.get("MessageId"), str):
            raise AgentChatLambdaConfigurationError("agent chat queue did not return MessageId")


def parse_message(body: object) -> JobKey:
    """Strict parse of the queue body; anything else is a poison message."""

    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("message is invalid")
    value = json.loads(body)
    if not isinstance(value, dict) or set(value) != _MESSAGE_KEYS:
        raise ValueError("message is invalid")
    return JobKey(tenant_id=value["tenantId"], matter_id=value["matterId"], job_id=value["jobId"])


def _enabled() -> bool:
    return os.environ.get("LEGALDESK_AGENT_CHAT_ENABLED", "false").strip().lower() == "true"


def _log(job_id: str, action: str, metadata: Mapping[str, object] | None) -> None:
    safe = sanitize_job_metadata(metadata)  # allowlisted keys with closed-shape values; no content, no nulls
    print(json.dumps({"event": "agent_chat_job", "jobId": job_id, "action": action, **safe}, separators=(",", ":"), sort_keys=True, default=str), flush=True)


def handle_sqs_event(
    event: Mapping[str, object],
    worker: Any,
    *,
    queue_arn: str,
    change_visibility: Callable[[str, int], None],
) -> dict[str, object]:
    records = event.get("Records") if isinstance(event, Mapping) else None
    if not isinstance(records, list) or not records:
        raise ValueError("SQS event is invalid")
    failures: list[dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId") if isinstance(record, Mapping) else None
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("SQS record is invalid")
        receipt = record.get("receiptHandle")
        try:
            if record.get("eventSourceARN") != queue_arn:
                raise ValueError("message did not come from the configured queue")
            key = parse_message(record.get("body"))
            result = worker.process(key)
        except Exception:
            # Poison or unexpected: one more delivery, then the DLQ (alarm).
            _retry_soon(change_visibility, receipt)
            failures.append({"itemIdentifier": message_id})
            _log("unknown", "error", None)
            continue
        _log(result.job_id, result.action, result.metadata)
        if result.action == "retry":
            _retry_soon(change_visibility, receipt)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def _retry_soon(change_visibility: Callable[[str, int], None], receipt: object) -> None:
    if isinstance(receipt, str) and receipt:
        try:
            change_visibility(receipt, RETRY_VISIBILITY_SECONDS)
        except Exception:
            pass  # the message reappears after the visibility timeout; the job deadline still bounds the work


def build_worker_from_environment() -> tuple[Any, str, Callable[[str, int], None]]:
    """Compose the worker from the same environment the API Lambda uses (no new secrets)."""

    import boto3
    from botocore.config import Config
    from legaldesk_agent import HarnessInvocationScope, HarnessInvoker, invoke_agent_chat

    from .agent_chat import AgentChatWorker
    from .application import AWSResourceConfig, build_aws_composition
    from .prompts import FileSystemSystemPromptProvider

    queue_url = os.environ.get("LEGALDESK_AGENT_CHAT_QUEUE_URL", "").strip()
    queue_arn = os.environ.get("LEGALDESK_AGENT_CHAT_QUEUE_ARN", "").strip()
    if not queue_url or not queue_arn:
        raise AgentChatLambdaConfigurationError("agent chat queue is not configured")
    config = AWSResourceConfig.from_environment()
    composition = build_aws_composition(allow_aws=True, config=config)
    runtime = composition.agent_chat
    if runtime is None:
        raise AgentChatLambdaConfigurationError("agent chat is not enabled in the composition")
    prompt_path = os.environ.get("LEGALDESK_AGENT_PROMPT_PATH")
    prompt = FileSystemSystemPromptProvider(Path(prompt_path) if prompt_path else default_agent_prompt_path()).load()
    # The loop may stream for up to 90 s (timeoutSeconds); the shared client's default
    # 60 s read timeout would cut a quiet stretch, so the worker owns its Harness client.
    harness_client = boto3.client(
        "bedrock-agentcore", region_name=config.region,
        config=Config(read_timeout=120, connect_timeout=5, retries={"total_max_attempts": 1, "mode": "standard"}),
    )
    invoker = HarnessInvoker(harness_client, config.harness_arn, telemetry_sink=composition.telemetry_sink)
    system_prompt = ({"text": prompt.content},)

    def invoke_agent(binding: Any, question: str, history: tuple[tuple[str, str], ...]) -> Any:
        return invoke_agent_chat(
            invoker, question, invocation_scope=HarnessInvocationScope.from_derived(binding),
            system_prompt=system_prompt, history=history,
        )

    worker = AgentChatWorker(
        job_store=runtime.job_store, state_store=composition.state_store, quota=composition.quota,
        authorization_store=composition.authorization_store, conversation_store=composition.conversation_store,
        grant_repository=composition.gateway_grant_repository, identity_verifier=composition.identity_verifier,
        gateway_url=composition.gateway_url, invoke_agent=invoke_agent,
        rag_chat=_rag_chat(composition), memory=composition.memory,
        prompt_identity=(prompt.version, prompt.sha256),
    )
    sqs = boto3.client("sqs", region_name=config.region, config=Config(retries={"total_max_attempts": 1, "mode": "standard"}))

    def change_visibility(receipt_handle: str, seconds: int) -> None:
        sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=seconds)

    return worker, queue_arn, change_visibility


def _rag_chat(composition: Any) -> Callable[..., Any]:
    def rag_chat(identity: Any, **kwargs: Any) -> Any:
        return composition.chat_service(identity, **kwargs)

    return rag_chat


_cached: tuple[Any, str, Callable[[str, int], None]] | None = None


def lambda_handler(
    event: Mapping[str, object],
    lambda_context: object,
    *,
    composed: tuple[Any, str, Callable[[str, int], None]] | None = None,
) -> dict[str, object]:
    global _cached
    if not _enabled():
        return {"status": "disabled", "batchItemFailures": []}
    if composed is None:
        if _cached is None:
            _cached = build_worker_from_environment()
        composed = _cached
    worker, queue_arn, change_visibility = composed
    remaining = getattr(lambda_context, "get_remaining_time_in_millis", None)
    worker.time_left = (lambda: remaining() / 1000.0) if callable(remaining) else None
    return handle_sqs_event(event, worker, queue_arn=queue_arn, change_visibility=change_visibility)


__all__ = [
    "AgentChatLambdaConfigurationError", "Boto3AgentChatQueue", "build_worker_from_environment",
    "default_agent_prompt_path",
    "handle_sqs_event", "lambda_handler", "parse_message",
]
