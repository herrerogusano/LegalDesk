# Phase 05 acceptance — versioned system prompt

## Criteria and local evidence

| Criterion | Evidence |
| --- | --- |
| Readable, explicitly versioned source of truth | [`prompts/legaldesk-system.md`](../prompts/legaldesk-system.md) has ID `legaldesk-system` and version `1.1.0`; no full copy is embedded in Python, documentation, or Phase 01 IaC. |
| Server-controlled, provider-neutral prompt loading | `backend/src/legaldesk/prompts.py` defines a provider interface and a filesystem implementation. The path is server configuration; browser payload parsing rejects prompt overrides. |
| Metadata, version, encoding, content, and size validation | `tests/test_prompts.py`: malformed/duplicate metadata, invalid version, missing content, invalid UTF-8, BOM, control characters, and over-limit artifact. |
| Prompt content is attached at the generation boundary | `tests/test_chat.py`: fake generator receives the loaded `SystemPromptArtifact` with validated content, version, and hash. |
| Response traceability and citation-aware insufficient evidence | `tests/test_chat.py`: `promptVersion` and `promptSha256` are returned for generated, cited partial-insufficient, and canonical no-evidence responses. Empty citations in an insufficient-evidence result remain canonical; cited partial explanations are preserved after citation validation. Malformed output and invalid citations still fail closed. Invalid prompt configuration does not invoke the generator. |
| Golden policy and output-contract coverage without model claims | `tests/test_prompts.py`: source policy, citations, insufficient evidence, advice limits/human review, document injection, prompt disclosure, privacy, tools, and authorization separation are checked as prompt structure. The JSON field names and evidence statuses are matched to the backend contract. No model inference was run. |
| No secrets or legal data | The prompt secret-pattern check passes; fixtures and test inputs are fictional. |

## Responsibilities and integration boundary

The prompt guides response behavior. It cannot grant or expand access and does
not implement authentication, ownership, IAM, matter authorization, or
retrieval filtering. Those controls remain deterministic server
responsibilities before evidence reaches generation. The browser cannot submit
prompt content or a prompt path.

The Phase 05 provider interface supplies a validated artifact to
`GenerationRequest`; a future real generator adapter must map its content to a
provider system-message field. The Phase 01 Harness keeps its minimal phase
demonstration prompt and is not wired to this application provider. No Phase 01
CloudFormation or AWS deployment configuration was changed.

## Cost, resources, and gaps

No AWS resources were created or modified. No retrieval, ingestion, or model
inference was run. Local tests have no AWS cost. Future Bedrock retrieval and
generation remain billable and require deliberate approval under
`AWS_COST_POLICY.md`.

There is no HTTP/API adapter, production generator, live AWS validation, or
model-based proof that a model follows these instructions. Prompt changes are
picked up by the filesystem provider without editing chat orchestration; a
deployed service must configure the artifact path and implement its own
provider adapter before it can use this prompt with a model.
