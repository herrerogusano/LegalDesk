"""LegalDesk backend domain package."""

from .authorization import (
    AuthorizationDenied,
    Boto3DynamoAuthorizationStore,
    InMemoryAuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
)
from .review_tasks import (
    AuthorizedToolEnvelope,
    CREATE_REVIEW_TASK_TOOL_DESCRIPTION,
    CREATE_REVIEW_TASK_TOOL_NAME,
    CREATE_REVIEW_TASK_TOOL_SCHEMA,
    Boto3DynamoReviewTaskRepository,
    InMemoryReviewTaskRepository,
    ReviewTaskLambdaHandler,
    ReviewReasonCode,
    create_review_task,
    create_review_task_for_identity,
    lambda_handler,
)

__all__ = [
    "AuthorizationDenied",
    "Boto3DynamoAuthorizationStore",
    "InMemoryAuthorizationStore",
    "RequestContext",
    "VerifiedIdentity",
    "build_request_context",
    "Boto3DynamoReviewTaskRepository",
    "AuthorizedToolEnvelope",
    "CREATE_REVIEW_TASK_TOOL_DESCRIPTION",
    "CREATE_REVIEW_TASK_TOOL_NAME",
    "CREATE_REVIEW_TASK_TOOL_SCHEMA",
    "InMemoryReviewTaskRepository",
    "ReviewTaskLambdaHandler",
    "ReviewReasonCode",
    "create_review_task",
    "create_review_task_for_identity",
    "lambda_handler",
]
