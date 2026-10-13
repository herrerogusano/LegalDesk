"""Callback-only Textract completion Lambda boundary."""

from __future__ import annotations

import os
from typing import Mapping

from .idp.ocr import Boto3DynamoOCRJobStore, Boto3TextractProvider, OCRCoordinator, OCRContractError, OCRStatus, _decode_event


class IDPOCRLambdaConfigurationError(RuntimeError):
    pass


def lambda_handler(event: Mapping[str, object], _lambda_context: object, *, coordinator: OCRCoordinator | None = None, processor: object | None = None) -> dict[str, object]:
    if os.environ.get("LEGALDESK_IDP_ENABLED", "false").strip().lower() != "true":
        return {"status": "disabled"}
    if coordinator is None:
        from .idp.runtime import build_runtime

        runtime = build_runtime(require_worker=False)
        payload = _decode_event(event)
        job_id = payload.get("JobId", payload.get("jobId"))
        if not isinstance(job_id, str):
            raise IDPOCRLambdaConfigurationError("OCR callback JobId is invalid")
        scope = Boto3DynamoOCRJobStore.locate_scope(runtime.processor.table, job_id)
        if scope is None:
            raise IDPOCRLambdaConfigurationError("OCR callback locator is unavailable")
        store = Boto3DynamoOCRJobStore(runtime.processor.table, tenant_id=scope[0], matter_id=scope[1])
        coordinator = OCRCoordinator(Boto3TextractProvider(runtime.processor.textract, notification_role_arn=runtime.processor.config.ocr_role_arn, notification_topic_arn=runtime.processor.config.ocr_topic_arn), store)
        processor = runtime.processor
        completion = coordinator.accept_completion(event, source_arn=runtime.processor.config.ocr_queue_arn)
    else:
        completion = coordinator.accept_completion(event)
    if processor is not None:
        if callable(getattr(processor, "set_lambda_context", None)):
            processor.set_lambda_context(_lambda_context)
        if completion.status is OCRStatus.SUCCEEDED:
            processor.continue_ocr(completion)
        elif completion.status in {OCRStatus.FAILED, OCRStatus.PARTIAL_SUCCESS}:
            processor.fail_ocr(completion)
    return {"status": completion.status.value, "runId": completion.run_id, "jobId": completion.textract_job_id}


__all__ = ["IDPOCRLambdaConfigurationError", "lambda_handler"]
