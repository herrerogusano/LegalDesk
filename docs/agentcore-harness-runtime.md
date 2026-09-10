# AgentCore Harness vs Runtime

Verified against the AWS documentation on 2026-09-10.

## Decision for LegalDesk

Phase 01 uses the managed AgentCore Harness in `eu-west-1`. Harness is now a
configuration-defined agent loop powered by Strands and executed on an
underlying AgentCore Runtime. That matches ADR-003 and avoids maintaining a
custom loop before LegalDesk needs one.

| Concern | Managed Harness | Custom Runtime |
|---|---|---|
| Orchestration loop | AWS manages model/tool/result/context loop | LegalDesk implements and tests it |
| Application artifact | Model, instructions, tools, limits config | Python entrypoint plus dependencies/container |
| Compute/session isolation | Underlying Runtime microVM per session | Runtime provides the same hosting boundary |
| AgentCore integrations | Primarily configuration | Explicit SDK calls in our code |
| Flexibility | Lower code burden, managed behavior | Full control with higher maintenance/security burden |

If Harness later prevents a required deterministic control, we can export or
move to custom Runtime after recording an architectural change. No such need
exists in Phase 01.

## What LegalDesk owns

- the system instruction and pinned model configuration;
- IAM execution/caller policies and deployment lifecycle;
- mapping authenticated users to session IDs and authorized request contexts;
- validation that strips caller-supplied model, prompt, tool, and skill
  overrides;
- application-side invocation adapter and output/error handling;
- tests, cost limits, teardown, and future authorization boundaries.

## Session isolation

Every new conversation receives a UUID (36 characters, satisfying AgentCore's
minimum 33-character session ID). The same ID continues a session; different
IDs request distinct Runtime session environments. Session IDs are correlation
handles, never authorization credentials. The backend must bind them to the
already-authorized user/matter before production use.

## Phase 01 restrictions

- No S3, Knowledge Base, Gateway, Memory, browser, code interpreter, MCP, or
  business tools.
- Harness built-in shell/file tools are blocked with a non-matching allowlist.
- Untrusted callers cannot override model, system prompt, tools, or skills.
- The execution role can invoke only the selected European Sonnet 4.6 profile
  and its six current destination model ARNs.
- Wildcard resources exist only where AWS actions do not support finer resource
  scoping (`ecr-public:GetAuthorizationToken` and
  `sts:GetServiceBearerToken`), plus log-group suffixes owned by this service.

## References

- AWS AgentCore Harness developer guide
- AWS Harness vs Runtime comparison
- AWS Harness security and execution role policy
- AWS CloudFormation `AWS::BedrockAgentCore::Harness` reference
