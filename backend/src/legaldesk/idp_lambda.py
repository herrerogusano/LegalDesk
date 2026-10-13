"""Disabled-by-default IDP SQS Lambda boundary.

Composition is intentionally lazy.  The handler accepts only the configured
queue ARN and delegates scope resolution to :class:`IDPWorker`; no queue
payload field is treated as tenant authorization.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .idp.worker import IDPWorker


class IDPLambdaConfigurationError(RuntimeError):
    pass


class Boto3IDPSQSQueue:
    def __init__(self, client: Any, *, queue_url: str) -> None:
        if client is None or not isinstance(queue_url, str) or not queue_url.strip():
            raise IDPLambdaConfigurationError("IDP queue configuration is invalid")
        self.client, self.queue_url = client, queue_url

    def publish(self, *, job_id: str) -> str:
        if not isinstance(job_id, str) or not job_id.strip():
            raise IDPLambdaConfigurationError("IDP job id is invalid")
        response = self.client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({"jobId": job_id}, separators=(",", ":")))
        message_id = response.get("MessageId") if isinstance(response, Mapping) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise IDPLambdaConfigurationError("IDP queue did not return MessageId")
        return message_id


def _enabled() -> bool:
    return os.environ.get("LEGALDESK_IDP_ENABLED", "false").strip().lower() == "true"


def lambda_handler(event: Mapping[str, object], _lambda_context: object, *, worker: IDPWorker | None = None) -> dict[str, object]:
    if not _enabled():
        return {"status": "disabled", "batchItemFailures": []}
    if worker is None:
        from .idp.runtime import build_runtime

        worker = build_runtime(require_worker=True).worker
    if worker is None:
        raise IDPLambdaConfigurationError("IDP worker composition is unavailable")
    processor = getattr(worker, "processor", None)
    if processor is not None and callable(getattr(processor, "set_lambda_context", None)):
        processor.set_lambda_context(_lambda_context)
    return worker.handle_sqs_event(event)


__all__ = ["Boto3IDPSQSQueue", "IDPLambdaConfigurationError", "lambda_handler"]
