"""Small loopback HTTP composition for the Phase 13 local integration gate.

This module deliberately uses the standard-library WSGI surface.  Provider
adapters are injected by callers/tests; the explicitly gated AWS composition
never silently falls back to local fakes.  The browser is a client
of this boundary, never an authority for identity, tenant, matter, document,
conversation, correlation, or tool scope.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

from .agent_integration import bind_harness_invocation
from .authorization import AuthorizationDenied, AuthorizationStore, VerifiedIdentity, build_request_context
from .documents import (
    DocumentError,
    DocumentPipeline,
    DocumentValidationError,
    InMemoryObjectStorage,
    UploadRequest,
    build_document_key,
)
from .domain.models import Document, DocumentStatus
from .identity import PkceAuthorizationRequest, create_pkce_authorization_request
from .ingestion import KnowledgeBaseSyncResult, run_knowledge_base_sync
from .mcp_server import GET_DOCUMENT_METADATA, LIST_MATTER_DOCUMENTS, MCPServer
from .memory import ConversationBindingStore, MemoryScope, derive_memory_scope_for_identity
from .observability import InMemoryTelemetrySink, TelemetrySink
from .gateway_interceptor import InMemoryGatewayGrantRepository
from .review_tasks import (
    InMemoryReviewTaskRepository,
    ReviewTaskError,
    ReviewReasonCode,
    MAX_REVIEW_NOTE_LENGTH,
    default_due_at,
)
from .state import (
    CitationHandle,
    EphemeralStateStore,
    InMemoryEphemeralStateStore,
    SessionRecord,
)


MAX_HTTP_BODY = 1_048_576
MAX_QUESTION = 1_000
REVIEW_CANDIDATE_TTL_SECONDS = 15 * 60
SESSION_COOKIE = "legaldesk_session"
STATE_COOKIE = "legaldesk_oauth_state"
CSRF_HEADER = "HTTP_X_CSRF_TOKEN"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class TokenExchange(Protocol):
    def exchange(self, code: str, *, code_verifier: str, redirect_uri: str) -> str: ...


class ChatService(Protocol):
    def __call__(
        self,
        identity: VerifiedIdentity,
        *,
        matter_id: str,
        conversation_id: str,
        session_id: str,
        question: str,
        correlation_id: str,
        authorized_evidence_sink: Callable[..., None] | None = None,
    ) -> Any: ...


class AuthorizedEvidenceSink(Protocol):
    def __call__(self, identity: VerifiedIdentity, context: Any, request: Any, response: Any, passages: tuple[Any, ...]) -> None: ...


class SyncService(Protocol):
    def __call__(self, *, identity: VerifiedIdentity, matter_id: str, document_ids: tuple[str, ...], correlation_id: str) -> Any: ...


class ApplicationTelemetrySink:
    """Forward safe events to the operator sink and retain a bounded audit view."""

    def __init__(self, downstream: TelemetrySink, *, max_events: int = 10_000) -> None:
        self.downstream = downstream
        self.local = InMemoryTelemetrySink(max_events=max_events)

    def record(self, event: Any) -> None:
        self.downstream.record(event)
        self.local.record(event)

    def by_correlation_id(self, correlation_id: str) -> tuple[Any, ...]:
        return self.local.by_correlation_id(correlation_id)


@dataclass(slots=True)
class ApplicationComposition:
    """All application dependencies; no provider is constructed implicitly."""

    identity_verifier: Any
    token_exchange: TokenExchange | None
    authorization_store: AuthorizationStore
    conversation_store: ConversationBindingStore
    document_pipeline: DocumentPipeline
    object_storage: Any
    metadata_repository: Any
    mcp_server: MCPServer
    review_repository: Any = None
    gateway_grant_repository: Any = None
    memory: Any = None
    chat_service: ChatService | None = None
    authorized_evidence_sink: AuthorizedEvidenceSink | None = None
    chat_accepts_evidence_sink: bool = False
    sync_service: SyncService | None = None
    telemetry_sink: TelemetrySink | None = None
    matter_catalog: tuple[str, ...] = ()
    authorization_endpoint: str = "https://example.invalid/oauth2/authorize"
    oauth_client_id: str = "legaldesk-local"
    redirect_uri: str = "http://localhost:8000/callback"
    public_base_url: str = "http://localhost:8000"
    gateway_url: str = "https://gateway.invalid/mcp"
    harness_invoker: Any = None
    gateway_invoker: Any = None
    harness_arn: str | None = None
    system_prompt: tuple[Mapping[str, object], ...] = ()
    session_ttl_seconds: int = 3_600
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"})
    allowed_origins: frozenset[str] = frozenset({"http://localhost:8000", "http://127.0.0.1:8000"})
    state_store: EphemeralStateStore | None = None

    def __post_init__(self) -> None:
        if not self.matter_catalog or len(self.matter_catalog) > 64 or any(not isinstance(item, str) or not item or len(item) > 128 for item in self.matter_catalog):
            raise ValueError("matter_catalog must be configured; catalog scans are forbidden")
        if self.telemetry_sink is None:
            self.telemetry_sink = InMemoryTelemetrySink()
        if self.state_store is None:
            self.state_store = InMemoryEphemeralStateStore()
        if self.review_repository is None:
            self.review_repository = InMemoryReviewTaskRepository()
        if self.gateway_grant_repository is None:
            self.gateway_grant_repository = InMemoryGatewayGrantRepository()


class LoopbackLegalDeskApp:
    """WSGI application with server-held authentication and scope records."""

    def __init__(self, composition: ApplicationComposition) -> None:
        self.composition = composition
        self.state_store = composition.state_store

    # Compatibility views for the existing offline tests.  The application
    # itself uses only the injected store; these are available only because the
    # in-memory store intentionally exposes its fixture state.
    @property
    def sessions(self) -> dict[str, SessionRecord]:
        return self._memory_store().sessions

    @property
    def oauth_states(self) -> dict[str, tuple[str, float]]:
        return self._memory_store().oauth_states

    @property
    def citation_handles(self) -> dict[str, CitationHandle]:
        return self._memory_store().citation_handles

    @property
    def _citation_links(self) -> dict[tuple[str, str, str, str], str]:
        return self._memory_store().citation_links

    @property
    def _conversation_selectors(self) -> dict[str, tuple[str, str, str, str]]:
        return self._memory_store().conversation_selectors

    @property
    def _conversation_correlations(self) -> dict[str, str]:
        return self._memory_store().conversation_correlations

    @property
    def _accepted_history_ids(self) -> dict[tuple[str, str, str], list[str]]:
        return self._memory_store().accepted_history_ids

    @property
    def _accepted_review_candidates(self) -> dict[tuple[str, str, str], tuple[float, dict[str, object]]]:
        return self._memory_store().accepted_review_candidates

    @property
    def _audit_records(self) -> list[dict[str, object]]:
        return self._memory_store().audit_records

    def _memory_store(self) -> InMemoryEphemeralStateStore:
        if not isinstance(self.state_store, InMemoryEphemeralStateStore):
            raise AttributeError("process-local state views are unavailable for durable stores")
        return self.state_store

    def _bound_state(self) -> None:
        """Bound server-side state through the injected state store."""

        self.state_store.bound_state()

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        status = HTTPStatus.OK
        headers: list[tuple[str, str]] = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            # These defaults apply to API errors as well as successful
            # responses, so a proxy cannot turn an operational failure into a
            # browser-rendered response or frame the loopback UI.
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Permissions-Policy", "camera=(), geolocation=(), microphone=()"),
        ]
        try:
            status, body, extra = self._dispatch(environ)
            if any(name.lower() == "content-type" for name, _value in extra):
                headers = [header for header in headers if header[0].lower() != "content-type"]
            headers.extend(extra)
        except AuthorizationDenied:
            status, body = HTTPStatus.FORBIDDEN, {"error": "access_denied"}
        except (DocumentValidationError, ValueError, KeyError):
            status, body = HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}
        except DocumentError:
            status, body = HTTPStatus.CONFLICT, {"error": "document_operation_failed"}
        except ReviewTaskError:
            status, body = HTTPStatus.BAD_REQUEST, {"error": "review_task_failed"}
        except Exception:
            # Never expose provider details, JWTs, prompts, or document data.
            status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "operation_failed"}
        if isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
        else:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers.append(("Content-Length", str(len(payload))))
        start_response(f"{status.value} {status.phrase}", headers)
        return [payload]

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> Mapping[str, Any]:
        length = environ.get("CONTENT_LENGTH", "0")
        try:
            size = int(length or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid body length") from exc
        if size < 0 or size > MAX_HTTP_BODY:
            raise ValueError("body too large")
        raw = environ.get("wsgi.input").read(size) if size else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("body must be JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError("body must be an object")
        return value

    @staticmethod
    def _cookies(environ: Mapping[str, Any]) -> SimpleCookie[str]:
        cookies: SimpleCookie[str] = SimpleCookie()
        cookies.load(environ.get("HTTP_COOKIE", ""))
        return cookies

    def _session(self, environ: Mapping[str, Any]) -> tuple[str, SessionRecord]:
        cookie = self._cookies(environ).get(SESSION_COOKIE)
        if cookie is None:
            raise AuthorizationDenied("access denied")
        key = cookie.value
        record = self.state_store.get_session(key)
        if record is None:
            raise AuthorizationDenied("access denied")
        try:
            # Re-verify on every request so token expiry and claim/signature
            # changes are not hidden by a long-lived local session.
            identity = self.composition.identity_verifier.verify_authorization_header(
                f"Bearer {record.access_token}"
            )
        except Exception as exc:
            self.state_store.delete_session(key)
            raise AuthorizationDenied("access denied") from exc
        return key, SessionRecord(identity, record.access_token, record.csrf_token, record.expires_at)

    def _request_guards(self, environ: Mapping[str, Any], *, session: SessionRecord | None, mutating: bool) -> None:
        host = str(environ.get("HTTP_HOST", "localhost")).split(":", 1)[0].lower()
        if host not in self.composition.allowed_hosts:
            raise AuthorizationDenied("invalid host")
        origin = environ.get("HTTP_ORIGIN")
        if origin and origin not in self.composition.allowed_origins:
            raise AuthorizationDenied("invalid origin")
        if mutating:
            if session is None:
                raise AuthorizationDenied("access denied")
            if environ.get(CSRF_HEADER) != session.csrf_token:
                raise AuthorizationDenied("csrf validation failed")

    def _context(self, identity: VerifiedIdentity, matter_id: str, correlation_id: str | None = None):
        return build_request_context(identity, matter_id, self.composition.authorization_store, correlation_id=correlation_id)

    @staticmethod
    def _document(document: Document) -> dict[str, object]:
        return {
            "documentId": document.document_id,
            "matterId": document.matter_id,
            "name": document.name,
            "mediaType": document.media_type,
            "status": document.status.value,
            "fileSizeBytes": document.file_size_bytes,
            "documentDate": document.document_date,
        }

    def _dispatch(self, environ: Mapping[str, Any]) -> tuple[HTTPStatus, Mapping[str, Any] | bytes, list[tuple[str, str]]]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = PurePosixPath(str(environ.get("PATH_INFO", "/")))
        self._request_guards(environ, session=None, mutating=False)
        static_assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/citations.js": ("citations.js", "text/javascript; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = static_assets.get(str(path))
        if asset is not None and method in {"GET", "HEAD"}:
            root = (Path(__file__).resolve().parents[3] / "frontend").resolve()
            target = (root / asset[0]).resolve()
            if target.parent != root or not target.is_file():
                raise KeyError("static asset")
            payload = target.read_bytes()
            return HTTPStatus.OK, payload if method == "GET" else b"", [
                ("Content-Type", asset[1]),
                ("Cache-Control", "no-store"),
            ]
        if path == PurePosixPath("/login") and method == "GET":
            return self._login()
        if path == PurePosixPath("/callback") and method == "GET":
            return self._callback(environ)
        if path == PurePosixPath("/logout") and method == "POST":
            key, session = self._session(environ)
            self._request_guards(environ, session=session, mutating=True)
            self.state_store.delete_session(key)
            return HTTPStatus.OK, {"ok": True}, [("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")]
        key, session = self._session(environ)
        mutating = method not in _SAFE_METHODS
        self._request_guards(environ, session=session, mutating=mutating)
        identity = session.identity
        parts = path.parts
        if path == PurePosixPath("/api/me") and method == "GET":
            user = self.composition.authorization_store.get_user_by_subject(identity.subject)
            return HTTPStatus.OK, {"subject": identity.subject, "userId": user.user_id if user else None, "csrfToken": session.csrf_token}, []
        if path == PurePosixPath("/api/matters") and method == "GET":
            matters = []
            for matter_id in self.composition.matter_catalog:
                try:
                    context = self._context(identity, matter_id)
                except AuthorizationDenied:
                    continue
                matter = self.composition.authorization_store.get_matter(context.matter_id)
                if matter is not None:
                    matters.append({"matterId": matter.matter_id, "name": matter.name, "status": matter.status.value})
            return HTTPStatus.OK, {"matters": matters}, []
        if path == PurePosixPath("/api/conversations") and method == "POST":
            data = self._body(environ)
            matter_id = data.get("matterId")
            if not isinstance(matter_id, str):
                raise ValueError("matterId is required")
            context = self._context(identity, matter_id)
            conversation_id, selector = str(uuid4()), f"s-{secrets.token_urlsafe(24)}"
            self.composition.conversation_store.bind(context, conversation_id=conversation_id, session_selector=selector)
            self.state_store.bind_conversation(
                conversation_id,
                (identity.subject, context.tenant_id, context.matter_id, selector),
                context.correlation_id,
            )
            self._audit(identity, context.matter_id, context.correlation_id, "conversation_created")
            return HTTPStatus.CREATED, {"conversationId": conversation_id, "sessionId": selector, "matterId": context.matter_id, "correlationId": context.correlation_id, "csrfToken": session.csrf_token}, []
        if len(parts) >= 4 and parts[1:3] == ("api", "matters"):
            matter_id = parts[3]
            if method == "POST" and len(parts) == 6 and parts[4:6] == ("documents", "upload-authorizations"):
                return self._authorize_upload(environ, identity, matter_id)
            if method == "GET" and len(parts) == 5 and parts[4] == "documents":
                context = self._context(identity, matter_id)
                docs = self.composition.document_pipeline.list_documents(identity, matter_id, correlation_id=context.correlation_id)
                return HTTPStatus.OK, {"documents": [self._document(item) for item in docs]}, []
            if method == "GET" and len(parts) == 6 and parts[4] == "documents":
                context = self._context(identity, matter_id)
                document = self.composition.metadata_repository.get_for_scope(
                    tenant_id=context.tenant_id,
                    matter_id=context.matter_id,
                    document_id=parts[5],
                )
                if document is None:
                    raise AuthorizationDenied("document access denied")
                return HTTPStatus.OK, {"document": self._document(document)}, []
            if method == "POST" and len(parts) == 7 and parts[4] == "documents" and parts[6] == "confirm":
                context = self._context(identity, matter_id)
                document = self.composition.document_pipeline.confirm_upload(identity, matter_id, parts[5], correlation_id=context.correlation_id)
                return HTTPStatus.OK, {"document": self._document(document)}, []
            if method == "POST" and len(parts) == 5 and parts[4] == "sync":
                data = self._body(environ)
                ids = data.get("documentIds", [])
                if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(item, str) for item in ids):
                    raise ValueError("documentIds is invalid")
                context = self._context(identity, matter_id)
                if self.composition.sync_service is None:
                    raise RuntimeError("document sync is not configured")
                result = self.composition.sync_service(identity=identity, matter_id=matter_id, document_ids=tuple(ids), correlation_id=context.correlation_id)
                safe_result = self._safe_result(result)
                operation_status = "documents_processing"
                if isinstance(result, KnowledgeBaseSyncResult):
                    operation_status = {
                        "COMPLETE": "documents_indexed" if result.documents_updated > 0 else "documents_processing",
                        "FAILED": "documents_failed",
                        "STOPPED": "documents_failed",
                        "TIMED_OUT": "documents_processing",
                    }.get(result.status, "documents_processing")
                return HTTPStatus.OK, {"operationStatus": operation_status, "result": safe_result}, []
            if method == "POST" and len(parts) == 5 and parts[4] == "review":
                return self._review(environ, identity, matter_id, session)
            if len(parts) == 5 and parts[4] == "reviews":
                if method == "GET":
                    return self._review_list(environ, identity, matter_id, session)
                if method == "POST":
                    return self._review_create(environ, identity, matter_id, session)
            if len(parts) == 6 and parts[4] == "reviews" and method == "GET":
                return self._review_get(environ, identity, matter_id, parts[5], session)
            if len(parts) == 6 and parts[4] == "reviews" and method in {"PATCH", "POST"}:
                return self._review_update(environ, identity, matter_id, parts[5], session)
        if path == PurePosixPath("/api/chat") and method == "POST":
            return self._chat(environ, identity)
        if len(parts) == 4 and parts[1:3] == ("api", "conversations") and parts[3] and method == "GET":
            return self._history(environ, identity, parts[3])
        if path == PurePosixPath("/api/citations") and method == "GET":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            return self._citation(identity, query.get("handle", [""])[0])
        if path == PurePosixPath("/api/mcp") and method == "POST":
            data = self._body(environ)
            matter_id = data.pop("matterId", None)
            conversation_id, session_id = data.pop("conversationId", None), data.pop("sessionId", None)
            if not all(isinstance(item, str) for item in (matter_id, conversation_id, session_id)):
                raise ValueError("matter, conversation, and session are required")
            if data.get("jsonrpc") != "2.0" or data.get("method") != "tools/call":
                raise ValueError("metadata request must be a JSON-RPC tools/call")
            params = data.get("params")
            if not isinstance(params, Mapping) or set(params) - {"name", "arguments", "_meta"}:
                raise ValueError("metadata tool request is invalid")
            tool_name, arguments = params.get("name"), params.get("arguments")
            if tool_name not in {LIST_MATTER_DOCUMENTS, GET_DOCUMENT_METADATA} or not isinstance(arguments, Mapping):
                raise ValueError("metadata tool request is invalid")
            if tool_name == LIST_MATTER_DOCUMENTS and arguments:
                raise ValueError("list tool accepts no arguments")
            if tool_name == GET_DOCUMENT_METADATA and (
                set(arguments) != {"documentId"}
                or not isinstance(arguments.get("documentId"), str)
                or not arguments["documentId"]
                or len(arguments["documentId"]) > 128
            ):
                raise ValueError("document selector is invalid")
            context = self._context(identity, matter_id)
            if not self.composition.conversation_store.is_bound(context=context, conversation_id=conversation_id, session_selector=session_id):
                raise AuthorizationDenied("conversation access denied")
            origin_correlation = data.pop("originCorrelationId", None)
            expected_correlation = self.state_store.get_conversation_correlation(conversation_id)
            if origin_correlation is not None and origin_correlation != expected_correlation:
                raise AuthorizationDenied("operation correlation is not owned")
            if expected_correlation is not None:
                context = self._context(identity, matter_id, correlation_id=expected_correlation)
            if tool_name == GET_DOCUMENT_METADATA and self.composition.metadata_repository.get_for_scope(
                tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=arguments["documentId"]
            ) is None:
                raise AuthorizationDenied("document access denied")
            binding = bind_harness_invocation(
                bearer_token=session.access_token,
                gateway_url=self.composition.gateway_url,
                identity_verifier=self.composition.identity_verifier,
                requested_matter_id=matter_id,
                conversation_id=conversation_id,
                session_selector=session_id,
                authorization_store=self.composition.authorization_store,
                conversation_store=self.composition.conversation_store,
                invocation_repository=self.composition.gateway_grant_repository,
                correlation_id=context.correlation_id,
                application_action="metadata",
            )
            result = self._invoke_gateway_tool(binding, tool_name=tool_name, arguments=arguments)
            self._audit(identity, matter_id, context.correlation_id, "mcp_metadata")
            self._audit_telemetry(identity, matter_id, context.correlation_id)
            return HTTPStatus.OK, result or {}, []
        if path == PurePosixPath("/api/audit") and method == "GET":
            records = []
            # Reauthorize each distinct matter once for this request, not once
            # per telemetry event. Never cache membership across requests.
            matter_access: dict[str, bool] = {}
            for item in self.state_store.list_audit(identity.subject, limit=1_000):
                if item.get("subject") != identity.subject:
                    continue
                matter_id = item.get("matterId")
                if not isinstance(matter_id, str):
                    continue
                if matter_id not in matter_access:
                    try:
                        self._context(identity, matter_id)
                        matter_access[matter_id] = True
                    except AuthorizationDenied:
                        matter_access[matter_id] = False
                if matter_access[matter_id]:
                    records.append(item)
            return HTTPStatus.OK, {"events": [self._safe_event(item) for item in records]}, []
        raise KeyError("route")

    def _login(self) -> tuple[HTTPStatus, Mapping[str, Any], list[tuple[str, str]]]:
        self._bound_state()
        request = create_pkce_authorization_request(
            self.composition.authorization_endpoint,
            client_id=self.composition.oauth_client_id,
            redirect_uri=self.composition.redirect_uri,
        )
        self.state_store.put_oauth_state(request.state, request.code_verifier, time.time() + 600)
        return HTTPStatus.FOUND, {}, [
            ("Location", request.authorization_url),
            ("Set-Cookie", f"{STATE_COOKIE}={request.state}; Max-Age=600; Path=/callback; HttpOnly; SameSite=Lax"),
        ]

    def _callback(self, environ: Mapping[str, Any]) -> tuple[HTTPStatus, Mapping[str, Any], list[tuple[str, str]]]:
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        state, code = query.get("state", [""])[0], query.get("code", [""])[0]
        cookie = self._cookies(environ).get(STATE_COOKIE)
        record = (
            self.state_store.consume_oauth_state(state)
            if state and cookie is not None and cookie.value == state
            else None
        )
        if not state or not code or cookie is None or cookie.value != state or record is None or self.composition.token_exchange is None:
            raise AuthorizationDenied("login failed")
        token = self.composition.token_exchange.exchange(code, code_verifier=record[0], redirect_uri=self.composition.redirect_uri)
        identity = self.composition.identity_verifier.verify_authorization_header(f"Bearer {token}")
        key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._bound_state()
        self.state_store.put_session(key, SessionRecord(identity, token, csrf, time.time() + self.composition.session_ttl_seconds))
        return HTTPStatus.FOUND, {}, [("Location", "/"), ("Set-Cookie", f"{SESSION_COOKIE}={key}; Max-Age={self.composition.session_ttl_seconds}; Path=/; HttpOnly; SameSite=Lax"), ("Set-Cookie", f"{STATE_COOKIE}=; Max-Age=0; Path=/callback; HttpOnly; SameSite=Lax")]

    def _authorize_upload(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str):
        data = self._body(environ)
        request = UploadRequest(filename=data.get("filename"), media_type=data.get("mediaType"), file_size_bytes=data.get("fileSizeBytes"), jurisdiction=data.get("jurisdiction", "fictional"), document_date=data.get("documentDate", "2099-01-01"), confidentiality=data.get("confidentiality", "fictional-internal"))
        context = self._context(identity, matter_id)
        authorization = self.composition.document_pipeline.initiate_upload(identity, matter_id, request, correlation_id=context.correlation_id)
        return HTTPStatus.CREATED, {"document": self._document(authorization.document), "presignedUrl": authorization.presigned_url, "uploadUrl": authorization.presigned_url, "method": authorization.method, "headers": dict(authorization.headers)}, []

    def _chat(self, environ: Mapping[str, Any], identity: VerifiedIdentity):
        data = self._body(environ)
        question, matter_id, conversation_id, session_id = data.get("question"), data.get("matterId"), data.get("conversationId"), data.get("sessionId")
        if not all(isinstance(value, str) for value in (question, matter_id, conversation_id, session_id)) or not question.strip() or len(question) > MAX_QUESTION:
            raise ValueError("question or scope is invalid")
        context = self._context(identity, matter_id)
        if not self.composition.conversation_store.is_bound(context=context, conversation_id=conversation_id, session_selector=session_id):
            raise AuthorizationDenied("conversation access denied")
        self.state_store.set_conversation_correlation(conversation_id, context.correlation_id)
        if self.composition.chat_service is None:
            raise RuntimeError("chat service is not configured")
        documents = self.composition.document_pipeline.list_documents(identity, matter_id, correlation_id=context.correlation_id)
        if any(document.status in {DocumentStatus.PENDING_UPLOAD, DocumentStatus.UPLOADED, DocumentStatus.PENDING_INGESTION} for document in documents):
            return HTTPStatus.OK, {"answer": "Los documentos seleccionados todavía se están procesando.", "citations": [], "evidenceStatus": None, "operationStatus": "documents_processing", "correlationId": context.correlation_id}, []
        if self.composition.chat_accepts_evidence_sink:
            response = self.composition.chat_service(identity, matter_id=matter_id, conversation_id=conversation_id, session_id=session_id, question=question, correlation_id=context.correlation_id, authorized_evidence_sink=self._store_authorized_evidence)
        else:
            response = self.composition.chat_service(identity, matter_id=matter_id, conversation_id=conversation_id, session_id=session_id, question=question, correlation_id=context.correlation_id)
        payload = response.to_dict() if hasattr(response, "to_dict") else response
        if not isinstance(payload, Mapping):
            raise RuntimeError("chat response is invalid")
        safe = dict(payload)
        citations = safe.get("citations")
        if isinstance(citations, list):
            safe["citations"] = [
                dict(
                    self._safe_citation(item, identity, context.matter_id, conversation_id),
                    handle=self.state_store.get_citation_link(
                        (identity.subject, conversation_id, context.correlation_id, str(item.get("citationId")))
                    ),
                )
                for item in citations
            ]
        if (
            safe.get("operationStatus") == "ok"
            and safe.get("evidenceStatus") in {"answerable", "ambiguous", "insufficient_evidence"}
            and isinstance(safe.get("answer"), str)
        ):
            self._remember_review_candidate(
                identity,
                conversation_id,
                context.correlation_id,
                question,
                safe,
            )
        self._audit(identity, context.matter_id, context.correlation_id, "chat")
        self._audit_telemetry(identity, context.matter_id, context.correlation_id)
        if self.composition.memory is not None and safe.get("operationStatus") not in {"error", "blocked"}:
            scope = derive_memory_scope_for_identity(identity, matter_id, conversation_id, session_id, self.composition.authorization_store, self.composition.conversation_store, correlation_id=context.correlation_id)
            evidence_status = safe.get("evidenceStatus")
            accepted = safe.get("operationStatus") == "ok" and evidence_status in {"answerable", "ambiguous", "insufficient_evidence"}
            if accepted:
                self._remember_history_event(identity.subject, conversation_id, session_id, self.composition.memory.append_event(scope, role="USER", text=question))
                answer = safe.get("answer")
                if isinstance(answer, str) and answer.strip():
                    self._remember_history_event(identity.subject, conversation_id, session_id, self.composition.memory.append_event(scope, role="ASSISTANT", text=answer))
        return HTTPStatus.OK, safe, []

    def _remember_review_candidate(
        self,
        identity: VerifiedIdentity,
        conversation_id: str,
        correlation_id: str,
        question: str,
        response: Mapping[str, object],
    ) -> None:
        """Keep only a bounded server-derived candidate for the next create.

        The injected store makes this candidate durable across app instances;
        the review Lambda still persists the validated snapshot atomically when
        a task is created.
        """

        citations: list[dict[str, object]] = []
        raw_citations = response.get("citations")
        if isinstance(raw_citations, list):
            for raw in raw_citations[:32]:
                if not isinstance(raw, Mapping):
                    continue
                handle_id = raw.get("handle")
                handle = (
                    self.state_store.get_citation(handle_id, subject=identity.subject)
                    if isinstance(handle_id, str)
                    else None
                )
                passage = handle.passage if handle is not None else None
                document_id = raw.get("documentId")
                if not isinstance(document_id, str) or not isinstance(passage, str):
                    continue
                citation = {
                    "citationId": raw.get("citationId", ""),
                    "documentId": document_id,
                    "documentName": raw.get("documentName"),
                    "pageNumber": raw.get("pageNumber"),
                    "section": raw.get("section"),
                    "passage": passage,
                }
                citations.append({key: value for key, value in citation.items() if value is not None})
        prompt_hash = hashlib.sha256(
            json.dumps(self.composition.system_prompt, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        candidate = {
            "question": question,
            "answer": response.get("answer"),
            "evidenceStatus": response.get("evidenceStatus"),
            "promptVersion": response.get("promptVersion"),
            "promptSha256": response.get("promptSha256"),
            "promptHash": prompt_hash,
            "citations": citations,
        }
        key = (identity.subject, conversation_id, correlation_id)
        self.state_store.put_review_candidate(
            key,
            (time.time() + REVIEW_CANDIDATE_TTL_SECONDS, candidate),
        )

    def _remember_history_event(self, subject: str, conversation_id: str, session_id: str, event: object) -> None:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) and isinstance(event, Mapping):
            event_id = event.get("eventId")
            nested = event.get("event")
            if not isinstance(event_id, str) and isinstance(nested, Mapping):
                event_id = nested.get("eventId")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            return
        self.state_store.add_history_event((subject, conversation_id, session_id), event_id)

    def _store_authorized_evidence(self, identity: VerifiedIdentity, context: Any, request: Any, response: Any, passages: tuple[Any, ...]) -> None:
        if len(passages) > 32:
            raise ValueError("too many citation passages")
        for passage in passages:
            self._bound_state()
            citation = getattr(passage, "citation", None)
            document_id = getattr(citation, "document_id", None)
            citation_id = getattr(citation, "citation_id", None)
            text = getattr(passage, "text", None)
            if not all(isinstance(value, str) and value for value in (document_id, citation_id, text)):
                raise ValueError("citation passage is malformed")
            handle_id = secrets.token_urlsafe(24)
            handle = CitationHandle(handle_id, identity.subject, context.tenant_id, context.matter_id, request.conversation_id, document_id, text, time.time() + 300)
            self.state_store.put_citation(
                handle,
                citation_key=(identity.subject, request.conversation_id, context.correlation_id, citation_id),
            )

    def _history(self, environ: Mapping[str, Any], identity: VerifiedIdentity, conversation_id: str):
        record = self.state_store.get_conversation(conversation_id)
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        requested_session = query.get("sessionId", [None])[0]
        if record is None or record[0] != identity.subject:
            raise AuthorizationDenied("conversation access denied")
        _subject, tenant_id, matter_id, selector = record
        if isinstance(requested_session, str):
            selector = requested_session
        context = self._context(identity, matter_id)
        if context.tenant_id != tenant_id:
            raise AuthorizationDenied("conversation access denied")
        if not self.composition.conversation_store.is_bound(
            context=context, conversation_id=conversation_id, session_selector=selector
        ):
            raise AuthorizationDenied("conversation access denied")
        if self.composition.memory is None:
            return HTTPStatus.OK, {"events": []}, []
        scope = derive_memory_scope_for_identity(identity, matter_id, conversation_id, selector, self.composition.authorization_store, self.composition.conversation_store, correlation_id=context.correlation_id)
        accepted_ids = self.state_store.list_history_ids((identity.subject, conversation_id, selector))
        if self.composition.memory.__class__.__name__.startswith("InMemory"):
            raw = self.composition.memory.list_events(scope)
            events = [{"eventId": item.event_id, "role": item.role, "text": item.text, "timestamp": item.event_timestamp.isoformat()} for item in raw if item.event_id in accepted_ids]
        else:
            raw = self.composition.memory.list_events(scope, maxResults=100, includePayloads=True)
            events = []
            provider_events = raw.get("events", ()) if isinstance(raw, Mapping) else ()
            if isinstance(provider_events, Sequence):
                for item in provider_events:
                    if not isinstance(item, Mapping):
                        continue
                    event_id = item.get("eventId")
                    if not isinstance(event_id, str) and isinstance(item.get("event"), Mapping):
                        event_id = item["event"].get("eventId")
                    if not isinstance(event_id, str) or event_id not in accepted_ids:
                        continue
                    source = item.get("event") if isinstance(item.get("event"), Mapping) else item
                    role, text = source.get("role"), source.get("text")
                    # AgentCore Memory returns nested conversational payloads;
                    # never treat arbitrary provider metadata as history text.
                    payload = source.get("payload")
                    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
                        for block in payload:
                            if not isinstance(block, Mapping):
                                continue
                            conversational = block.get("conversational")
                            if not isinstance(conversational, Mapping):
                                continue
                            candidate_role = conversational.get("role")
                            content = conversational.get("content")
                            candidate_text = content.get("text") if isinstance(content, Mapping) else None
                            if isinstance(candidate_role, str) and isinstance(candidate_text, str):
                                role, text = candidate_role, candidate_text
                                break
                    if isinstance(role, str) and isinstance(text, str) and len(text) <= 8_000:
                        events.append({"eventId": event_id, "role": role, "text": text})
        return HTTPStatus.OK, {"events": events[:100]}, []

    def _review_binding(
        self,
        identity: VerifiedIdentity,
        matter_id: str,
        session: SessionRecord,
        conversation_id: object,
        session_id: object,
        origin_correlation: object = None,
    ) -> tuple[Any, Any]:
        if not isinstance(conversation_id, str) or not isinstance(session_id, str):
            raise ValueError("conversation and session are required")
        context = self._context(identity, matter_id)
        if not self.composition.conversation_store.is_bound(context=context, conversation_id=conversation_id, session_selector=session_id):
            raise AuthorizationDenied("conversation access denied")
        expected_correlation = self.state_store.get_conversation_correlation(conversation_id)
        if origin_correlation is not None and origin_correlation != expected_correlation:
            raise AuthorizationDenied("operation correlation is not owned")
        if expected_correlation is not None:
            context = self._context(identity, matter_id, correlation_id=expected_correlation)
        binding = bind_harness_invocation(
            bearer_token=session.access_token,
            gateway_url=self.composition.gateway_url,
            identity_verifier=self.composition.identity_verifier,
            requested_matter_id=matter_id,
            conversation_id=conversation_id,
            session_selector=session_id,
            authorization_store=self.composition.authorization_store,
            conversation_store=self.composition.conversation_store,
            invocation_repository=self.composition.gateway_grant_repository,
            correlation_id=context.correlation_id,
            application_action="review",
        )
        return context, binding

    def _review_payload(self, result: Mapping[str, object], context: Any) -> Mapping[str, object]:
        payload = result.get("result") if isinstance(result, Mapping) else None
        if not isinstance(payload, Mapping):
            raise ReviewTaskError("review task unavailable")
        return dict(payload, correlationId=context.correlation_id)

    def _review_create(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str, session: SessionRecord):
        data = dict(self._body(environ))
        allowed = {"conversationId", "sessionId", "reasonCode", "note", "dueAt", "idempotencyKey", "originCorrelationId"}
        if set(data) - allowed or not isinstance(data.get("reasonCode"), str):
            raise ValueError("review request is invalid")
        try:
            ReviewReasonCode(data["reasonCode"])
        except (TypeError, ValueError) as exc:
            raise ValueError("review reason is invalid") from exc
        note = data.get("note", "")
        if not isinstance(note, str) or len(note) > MAX_REVIEW_NOTE_LENGTH:
            raise ValueError("review note is invalid")
        conversation_id, session_id = data.get("conversationId"), data.get("sessionId")
        context, binding = self._review_binding(identity, matter_id, session, conversation_id, session_id, data.get("originCorrelationId"))
        candidate_entry = self.state_store.get_review_candidate(
            (identity.subject, conversation_id, context.correlation_id)
        )
        candidate = candidate_entry[1] if candidate_entry else None
        if not isinstance(candidate, Mapping):
            # No accepted server-side answer means there is no review task to
            # create; client-supplied snapshots are deliberately ignored.
            raise ReviewTaskError("accepted answer unavailable")
        due_at = data.get("dueAt")
        if due_at is None:
            due_at = default_due_at().isoformat()
        arguments: dict[str, object] = {
            "reasonCode": data["reasonCode"],
            "note": note,
            "dueAt": due_at,
            "snapshot": dict(candidate),
        }
        if isinstance(data.get("idempotencyKey"), str):
            arguments["idempotencyKey"] = data["idempotencyKey"]
        result = self._invoke_gateway_tool(binding, tool_name="create_review_task", arguments=arguments)
        payload = self._review_payload(result, context)
        if not isinstance(payload.get("reviewTaskId"), str) or not isinstance(payload.get("status"), str):
            raise ReviewTaskError("review task unavailable")
        self._audit(identity, matter_id, context.correlation_id, "review_created")
        self._audit_telemetry(identity, matter_id, context.correlation_id)
        return HTTPStatus.CREATED, payload, []

    def _review_list(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str, session: SessionRecord):
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        context, binding = self._review_binding(identity, matter_id, session, query.get("conversationId", [None])[0], query.get("sessionId", [None])[0], query.get("originCorrelationId", [None])[0])
        raw_limit = query.get("limit", ["100"])[0]
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("review limit is invalid") from exc
        result = self._invoke_gateway_tool(binding, tool_name="list_review_tasks", arguments={"limit": limit})
        payload = self._review_payload(result, context)
        self._audit(identity, matter_id, context.correlation_id, "review_listed")
        return HTTPStatus.OK, payload, []

    def _review_get(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str, review_task_id: str, session: SessionRecord):
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        context, binding = self._review_binding(identity, matter_id, session, query.get("conversationId", [None])[0], query.get("sessionId", [None])[0], query.get("originCorrelationId", [None])[0])
        result = self._invoke_gateway_tool(binding, tool_name="get_review_task", arguments={"reviewTaskId": review_task_id})
        payload = self._review_payload(result, context)
        self._audit(identity, matter_id, context.correlation_id, "review_read")
        return HTTPStatus.OK, payload, []

    def _review_update(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str, review_task_id: str, session: SessionRecord):
        data = dict(self._body(environ))
        allowed = {"conversationId", "sessionId", "status", "resolutionNote", "originCorrelationId"}
        if set(data) - allowed or not isinstance(data.get("status"), str):
            raise ValueError("review update is invalid")
        context, binding = self._review_binding(identity, matter_id, session, data.pop("conversationId", None), data.pop("sessionId", None), data.pop("originCorrelationId", None))
        arguments = {"reviewTaskId": review_task_id, "status": data["status"]}
        if "resolutionNote" in data:
            arguments["resolutionNote"] = data["resolutionNote"]
        result = self._invoke_gateway_tool(binding, tool_name="update_review_task", arguments=arguments)
        payload = self._review_payload(result, context)
        self._audit(identity, matter_id, context.correlation_id, "review_updated")
        self._audit_telemetry(identity, matter_id, context.correlation_id)
        return HTTPStatus.OK, payload, []

    def _review(self, environ: Mapping[str, Any], identity: VerifiedIdentity, matter_id: str, session: SessionRecord):
        """Compatibility route retained for the Phase 13 manual demo."""

        return self._review_create(environ, identity, matter_id, session)

    def _invoke_harness_tool(self, binding: Any, request: Mapping[str, object], *, application_action: str, expected_tool: str | None = None) -> dict[str, object]:
        if self.composition.harness_invoker is None:
            raise RuntimeError("Harness is not configured")
        from legaldesk_agent import HarnessInvocationError, HarnessInvocationScope

        scope = HarnessInvocationScope.from_derived(binding)
        # The matter selector is model-visible request data, but it is never
        # an authority: Gateway still compares it with the server-owned
        # invocation binding before the target runs.
        request = dict(request)
        if application_action == "metadata":
            params = request.get("params")
            if isinstance(params, Mapping):
                params = dict(params)
                arguments = params.get("arguments")
                arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
                arguments["matterId"] = binding.matter_id
                params["arguments"] = arguments
                request["params"] = params
        elif application_action == "review":
            arguments = request.get("arguments")
            arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
            arguments["matterId"] = binding.matter_id
            request["arguments"] = arguments
        try:
            result = self.composition.harness_invoker.invoke(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                memory_scope=scope.memory_scope,
                correlation_id=scope.correlation_id,
                invocation_scope=scope,
            )
        except Exception:
            self._audit(binding.identity, binding.matter_id, scope.correlation_id, "harness_tool_error")
            self._audit_telemetry(binding.identity, binding.matter_id, scope.correlation_id)
            raise
        structured = getattr(result, "tool_results", ())
        if not isinstance(structured, tuple) or len(structured) != 1:
            raise HarnessInvocationError("Harness did not return exactly one structured tool result")
        tool_result = structured[0]
        if tool_result.status != "SUCCESS":
            raise ReviewTaskError("tool operation failed")
        allowed = set(getattr(binding, "allowed_tools", ()))
        allowed_short = {name.rsplit("___", 1)[-1] for name in allowed}
        if tool_result.name not in allowed and tool_result.name not in allowed_short:
            raise HarnessInvocationError("Harness returned an unallowed tool result")
        if expected_tool is not None and tool_result.name.rsplit("___", 1)[-1] != expected_tool:
            raise HarnessInvocationError("Harness returned the wrong tool result")
        if not isinstance(tool_result.payload, Mapping):
            raise HarnessInvocationError("Harness returned an invalid tool payload")
        return {"status": tool_result.status, "tool": tool_result.name, "result": dict(tool_result.payload), "correlationId": scope.correlation_id}

    def _invoke_gateway_tool(self, binding: Any, *, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Dispatch an explicit user action through Gateway exactly once."""

        if self.composition.gateway_invoker is None:
            raise RuntimeError("Gateway is not configured")
        try:
            result = self.composition.gateway_invoker.invoke(
                binding,
                tool_name=tool_name,
                arguments=dict(arguments),
            )
        except Exception:
            self._audit(binding.identity, binding.matter_id, binding.correlation_id, "gateway_tool_error")
            self._audit_telemetry(binding.identity, binding.matter_id, binding.correlation_id)
            raise
        if not isinstance(result, Mapping) or result.get("status") != "SUCCESS":
            raise RuntimeError("Gateway returned an invalid tool result")
        if result.get("tool") != f"@legaldesk_gateway/{'metadata-mcp' if tool_name in {LIST_MATTER_DOCUMENTS, GET_DOCUMENT_METADATA} else 'review-task-lambda'}___{tool_name}":
            raise RuntimeError("Gateway returned an unexpected tool")
        payload = result.get("result")
        if not isinstance(payload, Mapping) or result.get("correlationId") != binding.correlation_id:
            raise RuntimeError("Gateway returned an invalid tool payload")
        return {"status": "SUCCESS", "tool": result["tool"], "result": dict(payload), "correlationId": binding.correlation_id}

    def _audit(self, identity: VerifiedIdentity, matter_id: str, correlation_id: str, operation: str) -> None:
        self.state_store.append_audit({"subject": identity.subject, "matterId": matter_id, "correlationId": correlation_id, "operation": operation, "timestampMs": int(time.time() * 1000)})

    def _audit_telemetry(self, identity: VerifiedIdentity, matter_id: str, correlation_id: str) -> None:
        sink = self.composition.telemetry_sink
        by_correlation = getattr(sink, "by_correlation_id", None)
        if not callable(by_correlation):
            return
        for event in by_correlation(correlation_id):
            if hasattr(event, "to_dict"):
                safe = event.to_dict()
            elif isinstance(event, Mapping):
                safe = dict(event)
            else:
                continue
            self.state_store.append_audit({"subject": identity.subject, "matterId": matter_id, **safe})

    def _citation(self, identity: VerifiedIdentity, handle: str):
        record = self.state_store.get_citation(handle, subject=identity.subject)
        if record is None or record.expires_at <= time.time():
            raise AuthorizationDenied("citation access denied")
        context = self._context(identity, record.matter_id)
        if context.tenant_id != record.tenant_id:
            raise AuthorizationDenied("citation access denied")
        conversation = self.state_store.get_conversation(record.conversation_id)
        if conversation is None or conversation[0] != identity.subject or conversation[2] != record.matter_id:
            raise AuthorizationDenied("citation access denied")
        if not self.composition.conversation_store.is_bound(context=context, conversation_id=record.conversation_id, session_selector=conversation[3]):
            raise AuthorizationDenied("citation access denied")
        if record.document_id is not None:
            document = self.composition.metadata_repository.get_for_scope(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=record.document_id)
            if document is None or document.status is not DocumentStatus.INDEXED:
                raise AuthorizationDenied("citation access denied")
        return HTTPStatus.OK, {"handle": record.handle, "matterId": record.matter_id, "conversationId": record.conversation_id, "documentId": record.document_id, "passage": record.passage}, []

    @staticmethod
    def _safe_citation(item: object, identity: VerifiedIdentity, matter_id: str, conversation_id: str) -> Mapping[str, object]:
        if not isinstance(item, Mapping):
            return {}
        return {key: value for key, value in item.items() if key in {"citationId", "documentId", "documentName", "pageNumber", "section"} and isinstance(value, (str, int))}

    @staticmethod
    def _safe_result(value: object) -> object:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, KnowledgeBaseSyncResult):
            return {
                "ingestionJobId": value.ingestion_job_id,
                "status": value.status,
                "documentsUpdated": value.documents_updated,
                "failedDocumentCount": value.failed_document_count,
            }
        if isinstance(value, Mapping):
            return {str(key): LoopbackLegalDeskApp._safe_result(item) for key, item in value.items() if isinstance(key, str) and len(key) < 64}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [LoopbackLegalDeskApp._safe_result(item) for item in value[:32]]
        return None

    @staticmethod
    def _safe_event(value: object) -> Mapping[str, object]:
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            return {}
        allowed = {"subject", "matterId", "event_type", "outcome", "operation", "error_code", "correlation_id", "timestamp_ms", "latency_ms", "count", "prompt_version", "prompt_sha256", "resolver_prompt_version", "resolver_prompt_sha256", "writer_prompt_version", "writer_prompt_sha256", "correlationId", "timestampMs"}
        return {str(key): item for key, item in value.items() if key in allowed and isinstance(item, (str, int, float, bool))}


class _EmptyInput:
    def read(self, _size: int) -> bytes:
        return b""


def create_http_app(composition: ApplicationComposition) -> LoopbackLegalDeskApp:
    return LoopbackLegalDeskApp(composition)


def create_aws_composition(*, allow_aws: bool, config: Any | None = None) -> ApplicationComposition:
    """Compatibility entry point for the concrete AWS composition factory."""

    from .application import build_aws_composition

    return build_aws_composition(allow_aws=allow_aws, config=config)


__all__ = ["ApplicationComposition", "ApplicationTelemetrySink", "CitationHandle", "LoopbackLegalDeskApp", "TokenExchange", "create_aws_composition", "create_http_app"]
