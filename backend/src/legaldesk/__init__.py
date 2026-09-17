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
    gateway_lambda_handler,
    lambda_handler,
)
from .mcp_server import MCPServer, handle_metadata_request_for_identity, mcp_lambda_handler
from .gateway_interceptor import (
    Boto3DynamoGatewayGrantRepository,
    GatewayAuthorizationGrant,
    InMemoryGatewayGrantRepository,
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
    "gateway_lambda_handler",
    "lambda_handler",
    "MCPServer",
    "handle_metadata_request_for_identity",
    "mcp_lambda_handler",
    "Boto3DynamoGatewayGrantRepository",
    "GatewayAuthorizationGrant",
    "InMemoryGatewayGrantRepository",
]
