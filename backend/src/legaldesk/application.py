"""Production composition for the LegalDesk loopback/API entry point.

The factory is intentionally explicit: no provider client, JWKS resolver, or
resource adapter is constructed until ``allow_aws=True`` has been supplied by
the operator. Integration tests exercise this same factory with all external
provider constructors replaced by local doubles before the gate is enabled.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .authorization import Boto3DynamoAuthorizationStore
from .documents import Boto3DynamoDocumentMetadataRepository, Boto3S3ObjectStorage, DocumentPipeline
from .evidence import ConverseAnswerWriter, ConverseEvidenceResolver
from .gateway_interceptor import Boto3DynamoGatewayGrantRepository
from .gateway_client import DirectGatewayInvoker
from .guardrails import GuardrailConfig, GuardrailGroundingValidator
from .http_app import ApplicationComposition, ApplicationTelemetrySink
from .identity import OidcTokenVerifier, OidcVerifierConfig, PyJwtJwksKeyResolver
from .ingestion import DocumentScopeRef, run_knowledge_base_sync
from .mcp_server import MCPServer
from .memory import AgentCoreMemoryClient, Boto3DynamoConversationBindingStore
from .prompts import FileSystemSystemPromptProvider
from .review_tasks import Boto3DynamoReviewTaskRepository
from .observability import DEFAULT_TELEMETRY_SINK
from .smoke_budget import BudgetedSdkClient, SmokeBudget


@dataclass(frozen=True, slots=True)
class AWSResourceConfig:
    region: str
    metadata_table_name: str
    source_bucket_name: str
    knowledge_base_id: str
    data_source_id: str
    guardrail_identifier: str
    guardrail_version: str
    resolver_model_id: str
    writer_model_id: str
    harness_arn: str
    gateway_url: str
    memory_id: str
    jwks_url: str
    issuer: str
    client_id: str
    authorization_endpoint: str
    audience: str | None = None
    required_scope: str | None = "legaldesk/use"
    matter_catalog: tuple[str, ...] = ()
    prompt_path: Path | None = None
    token_endpoint: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "AWSResourceConfig":
        values = dict(os.environ if environ is None else environ)

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        catalog = tuple(item for item in values.get("LEGALDESK_MATTER_CATALOG", "").split(",") if item)
        if not catalog or len(catalog) > 64 or any(len(item) > 128 for item in catalog):
            raise ValueError("LEGALDESK_MATTER_CATALOG is required; authorization scans are forbidden")
        prompt = values.get("LEGALDESK_SYSTEM_PROMPT_PATH")
        return cls(
            region=values.get("AWS_REGION", "eu-west-1"),
            metadata_table_name=required("LEGALDESK_METADATA_TABLE_NAME"),
            source_bucket_name=required("LEGALDESK_SOURCE_BUCKET"),
            knowledge_base_id=required("LEGALDESK_KNOWLEDGE_BASE_ID"),
            data_source_id=required("LEGALDESK_DATA_SOURCE_ID"),
            guardrail_identifier=required("LEGALDESK_GUARDRAIL_ID"),
            guardrail_version=required("LEGALDESK_GUARDRAIL_VERSION"),
            resolver_model_id=required("LEGALDESK_RESOLVER_MODEL_ID"),
            writer_model_id=required("LEGALDESK_WRITER_MODEL_ID"),
            harness_arn=required("LEGALDESK_HARNESS_ARN"),
            gateway_url=required("LEGALDESK_GATEWAY_URL"),
            memory_id=required("LEGALDESK_MEMORY_ID"),
            jwks_url=required("LEGALDESK_JWKS_URL"),
            issuer=required("LEGALDESK_OIDC_ISSUER"),
            client_id=required("LEGALDESK_OIDC_CLIENT_ID"),
            authorization_endpoint=required("LEGALDESK_OIDC_AUTHORIZATION_ENDPOINT"),
            audience=values.get("LEGALDESK_OIDC_AUDIENCE") or None,
            required_scope=values.get("LEGALDESK_REQUIRED_SCOPE", "legaldesk/use") or None,
            matter_catalog=catalog,
            prompt_path=Path(prompt) if prompt else None,
            token_endpoint=required("LEGALDESK_OIDC_TOKEN_ENDPOINT"),
        )


class CognitoPkceTokenExchange:
    """Server-side authorization-code exchange; tokens never reach URLs/logs."""

    def __init__(self, endpoint: str, client_id: str) -> None:
        if not endpoint.startswith("https://") or not client_id:
            raise ValueError("token endpoint/client ID are invalid")
        self.endpoint, self.client_id = endpoint, client_id

    def exchange(self, code: str, *, code_verifier: str, redirect_uri: str) -> str:
        if not code or not code_verifier or not redirect_uri:
            raise ValueError("authorization code exchange is invalid")
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }).encode("ascii")
        request = urllib.request.Request(self.endpoint, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - endpoint is validated HTTPS config
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
        token = payload.get("access_token") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise ValueError("authorization exchange did not return an access token")
        return token


class _BedrockKnowledgeBaseSync:
    def __init__(self, client: Any, storage: Any, metadata: Any, auth_store: Any, config: AWSResourceConfig) -> None:
        self.client = client
        self.storage = storage
        self.metadata = metadata
        self.auth_store = auth_store
        self.config = config

    def __call__(self, *, identity: Any, matter_id: str, document_ids: tuple[str, ...], correlation_id: str) -> Any:
        from .authorization import build_request_context

        context = build_request_context(identity, matter_id, self.auth_store, correlation_id=correlation_id)
        # Authorization has already selected the request scope in the API;
        # load only explicit document IDs and derive tenant from the auth store
        # supplied to this service.
        if context is None:
            raise RuntimeError("sync authorization is not configured")
        refs = tuple(DocumentScopeRef(context.tenant_id, context.matter_id, document_id) for document_id in document_ids)
        return run_knowledge_base_sync(
            client=self.client,
            object_verifier=self.storage,
            metadata_repository=self.metadata,
            knowledge_base_id=self.config.knowledge_base_id,
            data_source_id=self.config.data_source_id,
            document_refs=refs,
            timeout_seconds=300,
            poll_interval_seconds=15,
        )


class _SeparatedOnlyGenerator:
    """Prevent production from silently falling back to the legacy seam."""

    def generate(self, _request: Any) -> Mapping[str, object]:
        raise RuntimeError("the separated resolver/writer pipeline is required")


class _BudgetedHarnessInvoker:
    """Consume validated Harness usage without changing default production."""

    def __init__(self, delegate: Any, budget: SmokeBudget) -> None:
        self._delegate = delegate
        self._budget = budget

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        result = self._delegate.invoke(*args, **kwargs)
        # A repeated/absent usage record is intentionally None and stops the
        # smoke budget rather than pretending the invocation was free.
        self._budget.consume_harness_usage(getattr(result, "usage", None))
        return result


def build_aws_composition(
    *,
    allow_aws: bool,
    config: AWSResourceConfig | None = None,
    smoke_budget: SmokeBudget | None = None,
    boto3_session: Any | None = None,
) -> ApplicationComposition:
    """Build the concrete AWS composition behind an explicit cost gate."""

    if allow_aws is not True:
        raise PermissionError("AWS composition requires explicit --allow-aws")
    # Keep the gate before importing boto3, creating a client, or constructing
    # a JWKS resolver. total_max_attempts=1 means zero SDK retries.
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 is required for AWS composition") from exc
    if smoke_budget is not None and not isinstance(smoke_budget, SmokeBudget):
        raise TypeError("smoke_budget must be a SmokeBudget")
    resource_config = config or AWSResourceConfig.from_environment()
    sdk_config = Config(retries={"total_max_attempts": 1, "mode": "standard"})
    client_factory = boto3.client if boto3_session is None else boto3_session.client
    resource_factory = boto3.resource if boto3_session is None else boto3_session.resource
    dynamodb = resource_factory("dynamodb", region_name=resource_config.region, config=sdk_config)
    table = dynamodb.Table(resource_config.metadata_table_name)
    s3 = client_factory("s3", region_name=resource_config.region, config=sdk_config)
    runtime = client_factory("bedrock-runtime", region_name=resource_config.region, config=sdk_config)
    knowledge_base = client_factory("bedrock-agent-runtime", region_name=resource_config.region, config=sdk_config)
    # Retrieval and ingestion are separate Bedrock APIs.  The runtime client
    # can retrieve from a KB, but Start/GetIngestionJob belong to the control
    # plane (bedrock-agent); accidentally sharing the runtime client would
    # fail only after a paid sync request reaches AWS.
    ingestion_client = client_factory("bedrock-agent", region_name=resource_config.region, config=sdk_config)
    agentcore = client_factory("bedrock-agentcore", region_name=resource_config.region, config=sdk_config)
    if smoke_budget is not None:
        table = BudgetedSdkClient(table, smoke_budget)
        s3 = BudgetedSdkClient(s3, smoke_budget)
        runtime = BudgetedSdkClient(runtime, smoke_budget)
        knowledge_base = BudgetedSdkClient(knowledge_base, smoke_budget)
        ingestion_client = BudgetedSdkClient(ingestion_client, smoke_budget)
        agentcore = BudgetedSdkClient(agentcore, smoke_budget)

    auth_store = Boto3DynamoAuthorizationStore(resource_config.metadata_table_name, table=table)
    metadata = Boto3DynamoDocumentMetadataRepository(resource_config.metadata_table_name, table=table, boto3_backed=True)
    storage = Boto3S3ObjectStorage(resource_config.source_bucket_name, client=s3)
    conversation_store = Boto3DynamoConversationBindingStore(resource_config.metadata_table_name, table=table)
    grant_repository = Boto3DynamoGatewayGrantRepository(resource_config.metadata_table_name, table=table)
    review_repository = Boto3DynamoReviewTaskRepository(resource_config.metadata_table_name, table=table)
    prompt_provider = FileSystemSystemPromptProvider(resource_config.prompt_path) if resource_config.prompt_path else FileSystemSystemPromptProvider()
    prompt = prompt_provider.load()
    guardrail_config = GuardrailConfig(resource_config.guardrail_identifier, resource_config.guardrail_version)
    resolver = ConverseEvidenceResolver(runtime, model_id=resource_config.resolver_model_id)
    writer = ConverseAnswerWriter(runtime, model_id=resource_config.writer_model_id)
    telemetry_sink = ApplicationTelemetrySink(DEFAULT_TELEMETRY_SINK)
    grounding = GuardrailGroundingValidator(runtime, guardrail_config, minimum_score=0.75, telemetry_sink=telemetry_sink)
    memory = AgentCoreMemoryClient(resource_config.memory_id, client=agentcore)
    gateway_invoker = DirectGatewayInvoker(
        resource_config.gateway_url,
        budget=smoke_budget,
    )
    verifier = OidcTokenVerifier(
        OidcVerifierConfig(
            issuer=resource_config.issuer,
            audience=resource_config.audience,
            client_id=resource_config.client_id,
            required_scope=resource_config.required_scope,
            # CognitoPkceTokenExchange stores only the OAuth access_token.
            # Do not widen the application boundary to ID tokens merely to
            # satisfy the verifier's optional dual-token configuration.
            allowed_token_use=frozenset({"access"}),
        ),
        PyJwtJwksKeyResolver(resource_config.jwks_url),
    )
    from legaldesk_agent import HarnessInvoker
    harness = HarnessInvoker(
        agentcore,
        resource_config.harness_arn,
        telemetry_sink=telemetry_sink,
        production_overrides=True,
        system_prompt=({"text": prompt.content},),
    )
    if smoke_budget is not None:
        harness = _BudgetedHarnessInvoker(harness, smoke_budget)
    pipeline = DocumentPipeline(auth_store, storage, metadata)
    sync = _BedrockKnowledgeBaseSync(ingestion_client, storage, metadata, auth_store, resource_config)
    composition = ApplicationComposition(
        identity_verifier=verifier,
        token_exchange=CognitoPkceTokenExchange(resource_config.token_endpoint, resource_config.client_id) if resource_config.token_endpoint else None,
        authorization_store=auth_store,
        conversation_store=conversation_store,
        document_pipeline=pipeline,
        object_storage=storage,
        metadata_repository=metadata,
        mcp_server=MCPServer(metadata),
        review_repository=review_repository,
        gateway_grant_repository=grant_repository,
        memory=memory,
        telemetry_sink=telemetry_sink,
        matter_catalog=resource_config.matter_catalog,
        oauth_client_id=resource_config.client_id,
        authorization_endpoint=resource_config.authorization_endpoint,
        gateway_url=resource_config.gateway_url,
        harness_invoker=harness,
        gateway_invoker=gateway_invoker,
        system_prompt=({"text": prompt.content},),
        sync_service=sync,
    )
    from .chat import ChatRequest, answer_question

    def chat_service(identity: Any, *, matter_id: str, conversation_id: str, session_id: str, question: str, correlation_id: str, authorized_evidence_sink: Any = None) -> Any:
        return answer_question(
            identity,
            ChatRequest(conversation_id, session_id, matter_id, question),
            authorization_store=auth_store,
            retrieval_client=knowledge_base,
            knowledge_base_id=resource_config.knowledge_base_id,
            generator=_SeparatedOnlyGenerator(),
            guardrail_client=runtime,
            guardrail_config=guardrail_config,
            conversation_binding_store=conversation_store,
            correlation_id=correlation_id,
            evidence_resolver=resolver,
            answer_writer=writer,
            grounding_validator=grounding,
            prompt_provider=prompt_provider,
            telemetry_sink=composition.telemetry_sink,
            authorized_evidence_sink=authorized_evidence_sink,
        )

    composition.chat_service = chat_service
    composition.chat_accepts_evidence_sink = True
    return composition


__all__ = ["AWSResourceConfig", "build_aws_composition"]
