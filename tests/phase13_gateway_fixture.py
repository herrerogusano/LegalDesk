"""Local managed-Harness/Gateway transport double, with real target handlers.

AWS owns the real transport and JWT authorizer. This double verifies the real
signed token before invoking the actual interceptor, MCP and Review handlers.
It does not establish live IAM or provider interoperability.
"""
import json
from unittest.mock import patch

from legaldesk.gateway_interceptor import GatewayTarget, transform_gateway_request
from legaldesk.mcp_server import mcp_lambda_handler
from legaldesk.review_tasks import gateway_lambda_handler


def invoke_local_gateway(composition, calls, **kwargs):
    calls.append(kwargs)
    remote = kwargs["tools"][0]["config"]["remoteMcp"]
    assert remote["url"] == composition.gateway_url
    headers = remote["headers"]
    identity = composition.identity_verifier.verify_authorization_header(headers["Authorization"])
    assert identity.subject
    assert kwargs["actorId"] == headers["x-legaldesk-memory-actor-id"]
    request = json.loads(kwargs["messages"][0]["content"][0]["text"])
    if request.get("action") == "create_review_task":
        target = GatewayTarget.REVIEW_LAMBDA
        name, arguments = "create_review_task", request["arguments"]
    else:
        target = GatewayTarget.METADATA_MCP
        name, arguments = request["params"]["name"], request["params"]["arguments"]
    qualified = f"@legaldesk_gateway/{target.value}___{name}"
    assert qualified in kwargs["allowedTools"]
    # The real model cannot see transport headers. The backend must supply the
    # authorized selector as task data; the interceptor still treats it as
    # untrusted and compares it to the server-held invocation binding.
    assert arguments["matterId"] == headers["x-legaldesk-requested-matter-id"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    transformed = transform_gateway_request(
        {"mcp": {"gatewayRequest": {"headers": headers, "body": body}, "rawGatewayRequest": {"body": json.dumps(body)}}},
        target=target, authorization_store=composition.authorization_store,
        grant_repository=composition.gateway_grant_repository, telemetry_sink=composition.telemetry_sink,
    )["mcp"]["transformedGatewayRequest"]
    with patch("legaldesk.observability.DEFAULT_TELEMETRY_SINK", composition.telemetry_sink):
        if target == GatewayTarget.METADATA_MCP:
            with patch("legaldesk.mcp_server._mcp_repositories_from_environment", return_value=(composition.authorization_store, composition.metadata_repository)), patch("legaldesk.mcp_server._mcp_grant_repository_from_environment", return_value=composition.gateway_grant_repository):
                response = mcp_lambda_handler({"headers": transformed["headers"], "body": json.dumps(transformed["body"])}, None)
            payload = json.loads(response["body"])
            if response["statusCode"] != 200 or "result" not in payload:
                raise RuntimeError("local MCP target rejected")
            payload = payload["result"]
        else:
            with patch("legaldesk.review_tasks._repositories_from_environment", return_value=(composition.review_repository, composition.authorization_store)), patch("legaldesk.review_tasks._gateway_grant_repository_from_environment", return_value=composition.gateway_grant_repository):
                payload = gateway_lambda_handler(transformed["body"]["params"]["arguments"], None)
            if "error" in payload:
                raise RuntimeError("local Review target rejected")
    return {"stream": [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "tool-local", "name": qualified, "type": "mcp_tool_use"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStart": {"role": "user"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolResult": {"toolUseId": "tool-local", "status": "success"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"json": payload}]}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]}
