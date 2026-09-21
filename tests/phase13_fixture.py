"""Provider-shaped, network-free Phase 13 integration fixture.

The fixture deliberately replaces SDK clients at the composition boundary. It
does not provide application answers or bypass authorization; retrieval,
Converse, Guardrail, Dynamo, S3, and Harness calls remain observable fake
provider operations used only by integration tests.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from legaldesk.application import AWSResourceConfig, build_aws_composition
from legaldesk.authorization import authorization_matter_partition_key, authorization_user_partition_key
from legaldesk.documents import document_partition_key


class FakeDynamoTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def get_item(self, *, Key: Mapping[str, str], **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("get_item", dict(Key)))
        item = self.items.get((str(Key["pk"]), str(Key["sk"])))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, *, Item: Mapping[str, object], **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("put_item", dict(Item)))
        key = (str(Item["pk"]), str(Item["sk"]))
        if kwargs.get("ConditionExpression") and key in self.items:
            raise RuntimeError("conditional check failed")
        self.items[key] = dict(Item)
        return {}

    def update_item(self, *, Key: Mapping[str, str], ExpressionAttributeValues: Mapping[str, object], **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("update_item", dict(Key)))
        key = (str(Key["pk"]), str(Key["sk"]))
        if key not in self.items:
            raise RuntimeError("missing item")
        self.items[key]["status"] = ExpressionAttributeValues[":status"]
        return {}

    def query(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("query", {}))
        expression = kwargs["KeyConditionExpression"].get_expression()["values"]
        partition = expression[0].get_expression()["values"][1]
        prefix = expression[1].get_expression()["values"][1]
        return {"Items": [dict(item) for (pk, sk), item in self.items.items() if pk == partition and sk.startswith(prefix)]}


class FakeDynamoResource:
    def __init__(self, table: FakeDynamoTable) -> None:
        self.table = table

    def Table(self, _name: str) -> FakeDynamoTable:
        return self.table


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}
        self.put_calls: list[str] = []
        self.put_server: _PresignedPutServer | None = None

    def generate_presigned_url(self, _operation: str, *, Params: Mapping[str, object], ExpiresIn: int, HttpMethod: str) -> str:
        if self.put_server is None:
            raise RuntimeError("presigned server not configured")
        key = str(Params["Key"])
        token = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        return f"http://127.0.0.1:{self.put_server.port}/put/{token}"

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: Mapping[str, str], **kwargs: object) -> None:
        self.put_calls.append(Key)
        self.objects[Key] = (bytes(Body), ContentType, dict(Metadata))

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        body, content_type, metadata = self.objects[Key]
        return {"ContentLength": len(body), "ContentType": content_type, "Metadata": dict(metadata)}

    def put_external(self, key: str, body: bytes, headers: Mapping[str, str]) -> None:
        metadata = {
            name[len("x-amz-meta-"):].lower(): value
            for name, value in headers.items()
            if name.lower().startswith("x-amz-meta-")
        }
        self.objects[key] = (body, headers.get("Content-Type", "application/octet-stream"), metadata)


class _PutHandler(BaseHTTPRequestHandler):
    server: "_PresignedPutServer"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.server.allowed_origin)
        self.send_header("Access-Control-Allow-Methods", "PUT")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,x-amz-meta-tenant-id,x-amz-meta-matter-id,x-amz-meta-document-id,x-amz-server-side-encryption")
        self.end_headers()

    def do_PUT(self) -> None:  # noqa: N802
        token = self.path.rsplit("/", 1)[-1]
        key = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.client.put_external(key, body, self.headers)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self.server.allowed_origin)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


class _PresignedPutServer:
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _PutHandler)
        self.server.client = client
        self.server.allowed_origin = "http://localhost:8000"
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class FakeKnowledgeBaseRuntime:
    def __init__(self, table: FakeDynamoTable, storage: FakeS3Client) -> None:
        self.table = table
        self.storage = storage
        self.retrieve_calls: list[dict[str, object]] = []
        self.converse_calls: list[dict[str, object]] = []
        self.guardrail_calls: list[dict[str, object]] = []
        self.mode = "answerable"
        self.fail_retrieve = False

    def retrieve(self, **kwargs: object) -> Mapping[str, object]:
        self.retrieve_calls.append(kwargs)
        if self.fail_retrieve:
            raise RuntimeError("provider retrieval failure")
        query = kwargs.get("retrievalQuery", {})
        question = str(query.get("text", "")) if isinstance(query, Mapping) else ""
        if "none" in question.lower():
            return {"retrievalResults": []}
        filter_value = kwargs.get("retrievalConfiguration", {})
        matter_id = ""
        try:
            matter_id = str(filter_value["vectorSearchConfiguration"]["filter"]["andAll"][1]["equals"]["value"])
        except (KeyError, TypeError):
            return {"retrievalResults": []}
        docs = [item for item in self.table.items.values() if item.get("entityType") == "Document" and item.get("matterId") == matter_id and item.get("status") == "INDEXED"]
        if not docs:
            return {"retrievalResults": []}
        document = docs[0]
        body = self.storage.objects.get(str(document["s3Key"]), (b"", "", {}))[0]
        passage_text = body.decode("utf-8")
        return {"retrievalResults": [{
            "content": {"text": passage_text},
            "location": {"s3Location": {"uri": f"s3://fictional/{document['documentId']}.pdf"}},
            "metadata": {"tenantId": document["tenantId"], "matterId": matter_id, "documentId": document["documentId"], "documentName": document["name"]},
            "score": 0.91,
        }]}

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.converse_calls.append(kwargs)
        text = str(kwargs["messages"][0]["content"][0]["text"])
        payload = json.loads(text)
        if payload.get("task") == "resolve_evidence_only":
            question = str(payload.get("question", ""))
            if "none" in question.lower():
                result = {"coverage": "none", "conflict": False, "supportingCitationIds": []}
            elif "partial" in question.lower():
                result = {"coverage": "partial", "conflict": False, "supportingCitationIds": ["citation-1"]}
            else:
                result = {"coverage": "complete", "conflict": False, "supportingCitationIds": ["citation-1"]}
        elif payload.get("mode") == "separated_answer_writer":
            result = {"answer": "The inspection period is four years."}
        else:
            result = {"answer": "The inspection period is four years.", "citationIds": ["citation-1"], "evidenceStatus": "answerable"}
        return {"output": {"message": {"content": [{"text": json.dumps(result)}]}}}

    def apply_guardrail(self, **kwargs: object) -> Mapping[str, object]:
        self.guardrail_calls.append(kwargs)
        if kwargs.get("source") != "OUTPUT":
            return {"action": "NONE", "outputs": [], "assessments": []}
        return {"action": "NONE", "outputs": [], "assessments": [{"contextualGroundingPolicy": {"filters": [
            {"type": "GROUNDING", "score": 0.91, "threshold": 0.75, "action": "NONE"},
            {"type": "RELEVANCE", "score": 0.91, "threshold": 0.5, "action": "NONE"},
        ]}}]}


class FakeIngestionClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def start_ingestion_job(self, **kwargs: object) -> Mapping[str, object]:
        self.start_calls.append(kwargs)
        return {"ingestionJob": {"ingestionJobId": "job-integration"}}

    def get_ingestion_job(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(kwargs)
        return {"ingestionJob": {"ingestionJobId": "job-integration", "status": "COMPLETE", "statistics": {"numberOfDocumentsFailed": 0}}}


class FakeAgentCoreClient:
    def __init__(self) -> None:
        self.invoke_calls: list[dict[str, object]] = []
        self.events: dict[tuple[str, str], list[dict[str, object]]] = {}

    def invoke_harness(self, **kwargs: object) -> Mapping[str, object]:
        self.invoke_calls.append(kwargs)
        # The Harness stream is intentionally left to the integration test's
        # real Gateway double; this provider-shaped client records request
        # overrides and can be replaced by that target router there.
        return {"stream": []}

    def create_event(self, *, actorId: str, sessionId: str, payload: object, **kwargs: object) -> Mapping[str, object]:
        event_id = f"evt-{len(self.events.get((actorId, sessionId), [])) + 1}"
        item = {"eventId": event_id, "payload": payload}
        self.events.setdefault((actorId, sessionId), []).append(item)
        return {"event": item}

    def list_events(self, *, actorId: str, sessionId: str, **kwargs: object) -> Mapping[str, object]:
        return {"events": list(self.events.get((actorId, sessionId), []))}


class FakeKeyResolver:
    def __init__(self, public_key: object) -> None:
        self.public_key = public_key

    def get_signing_key(self, _token: str) -> object:
        return self.public_key


class FakeTokenExchange:
    token: str = ""

    def __init__(self, _endpoint: str, _client_id: str) -> None:
        pass

    def exchange(self, code: str, *, code_verifier: str, redirect_uri: str) -> str:
        if code != "integration-code" or not code_verifier or not redirect_uri:
            raise ValueError("token exchange failed")
        return self.token


class Phase13ProviderFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()
        self.table = FakeDynamoTable()
        self.s3 = FakeS3Client()
        self.put_server = _PresignedPutServer(self.s3)
        self.s3.put_server = self.put_server
        self.runtime = FakeKnowledgeBaseRuntime(self.table, self.s3)
        self.ingestion = FakeIngestionClient()
        self.agentcore = FakeAgentCoreClient()
        self.token = self._token("alice")
        self._seed_auth()

    def _token(self, subject: str, *, expired: bool = False) -> str:
        now = int(time.time())
        return jwt.encode({"sub": subject, "iss": "https://issuer.integration", "exp": now - 600 if expired else now + 600, "iat": now - 1200 if expired else now - 1, "token_use": "access", "client_id": "integration-client", "aud": "integration-audience", "scope": "openid legaldesk/use"}, self.private_key, algorithm="RS256", headers={"kid": "integration-key"})

    def _seed_auth(self) -> None:
        self.table.items[(authorization_user_partition_key("alice"), "PROFILE")] = {"pk": authorization_user_partition_key("alice"), "sk": "PROFILE", "entityType": "User", "userId": "user-alice", "verifiedSubject": "alice", "tenantIds": ["tenant-integration"], "roles": ["member"]}
        self.table.items[(authorization_matter_partition_key("matter-integration"), "PROFILE")] = {"pk": authorization_matter_partition_key("matter-integration"), "sk": "PROFILE", "entityType": "Matter", "matterId": "matter-integration", "tenantId": "tenant-integration", "name": "Integration Matter", "authorizedUserIds": ["user-alice"], "status": "active"}

    def config(self) -> AWSResourceConfig:
        return AWSResourceConfig(region="eu-west-1", metadata_table_name="integration-table", source_bucket_name="integration-bucket", knowledge_base_id="integration-kb", data_source_id="integration-source", guardrail_identifier="integration-guardrail", guardrail_version="1", resolver_model_id="integration-resolver", writer_model_id="integration-writer", harness_arn="arn:aws:bedrock-agentcore:eu-west-1:123:harness/integration", gateway_url="https://gateway.integration.test/mcp", memory_id="integration-memory", jwks_url="https://issuer.integration/jwks", issuer="https://issuer.integration", client_id="integration-client", audience="integration-audience", authorization_endpoint="https://issuer.integration/authorize", matter_catalog=("matter-integration",), prompt_path=self.root / "prompts" / "legaldesk-system.md", token_endpoint="https://issuer.integration/token")

    def close(self) -> None:
        self.put_server.close()
