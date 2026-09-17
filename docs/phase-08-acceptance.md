# Phase 08 acceptance

| Criterion | Evidence | Status |
|---|---|---|
| MCP tools/list and tools/call | `tests/test_mcp_server.py` | PASS (local) |
| MCP lifecycle and schema guard | `tests/test_mcp_server.py` | PASS (local) |
| Current-matter listing and metadata | `tests/test_mcp_server.py` | PASS (local) |
| Invalid/cross-matter requests fail safely | MCP and authorization tests | PASS (local) |
| Metadata excludes document bodies and S3 keys | MCP serialization regression tests | PASS (local) |
| Review target reauthorizes before persistence | `tests/test_review_tasks.py` entrypoint tests | PASS (local) |
| REQUEST interceptor derives/overwrites trusted context | `tests/test_gateway_interceptor.py` | PASS (local) |
| Deterministic MCP/Lambda tool routing | `tests/test_agentcore_phase_01.py` | PASS (local) |
| Reproducible Gateway and target IaC | `tests/test_phase_08_gateway_iac.py` | PASS (static local) |
| Existing Harness attachment fragment and scoped InvokeGateway | `tests/test_phase_08_agent_attachment.py` | PASS (static local) |
| Gateway live call reaches MCP and Lambda | AWS deployment/smoke | PENDING permission/deployment |
| Verified grant/context propagation through Gateway targets | AWS interceptor/flat Lambda target contract validation | PENDING; fail closed until proven |

Local tests use fictional in-memory repositories and never invoke AWS, a real
model, Gateway, Lambda, Function URL, or MCP network endpoint. Phase 09 is not
started.
