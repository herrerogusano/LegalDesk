# Phase 06 acceptance — Bedrock Guardrails

This phase is validated locally with fakes and CloudFormation text checks. No
AWS resource was deployed and no `ApplyGuardrail` request or model inference
was executed.

| Criterion | Expected | Actual local evidence | Status |
| --- | --- | --- | --- |
| Prompt injection / jailbreak | Input intervention blocks before retrieval or generation | `tests/test_chat.py` asserts blocked input and call timeline | PASS (mock) |
| PII handling | Anonymized input is used for retrieval; anonymized output is returned | Input/output PII tests assert masked text and no sensitive audit content | PASS (mock) |
| Individualized advice / harmful content | Denied topic and harmful categories block before retrieval | Advice and violence scenarios assert input block | PASS (mock) |
| Contextual grounding | Query, authorized sources, and answer use required qualifiers; unsupported output blocks | Output payload and grounding intervention tests assert qualifiers and block | PASS (mock) |
| Authorization boundary | Cross-matter request is denied before every guardrail call | Cross-matter and unverified identity tests assert zero Guardrail calls | PASS (local) |
| Fail closed | Client errors, malformed actions/assessments, inconsistent action, and missing masked output stop processing | `tests/test_guardrails.py` covers parser and error cases | PASS (local) |
| Safe audit trail | Record correlation/stage/action/outcome without prompt, PII, evidence, or secrets | Audit sink and logging regression tests inspect emitted fields | PASS (local) |
| IaC and least privilege | Template contains only Guardrail + immutable version; runtime permission is scoped to that ARN | `tests/test_phase_06_guardrail_iac.py` checks resource types, policies, thresholds, and no wildcard IAM | PASS (static) |
| AWS smoke / thresholds | Representative real calls validate expected behavior and thresholds | Not run; requires explicit cost approval and synthetic data | NOT RUN |

Guardrails are a probabilistic safety layer and do not provide authentication,
ownership, matter authorization, or retrieval filtering. The backend keeps
those decisions deterministic and upstream. AWS Guardrails invocations and
the two CloudFormation resources may incur charges; deployment and smoke
validation remain a later, explicitly authorized action.
