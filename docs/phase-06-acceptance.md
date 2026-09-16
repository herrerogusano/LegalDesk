# Phase 06 acceptance — Bedrock Guardrails

This phase was validated locally and with a small AWS smoke in `eu-west-1`.
Only synthetic text was sent to `ApplyGuardrail`; no model inference was
executed. The stack was created successfully, then deleted successfully, and
CloudFormation reported no remaining stack resources.

| Criterion | Expected | Evidence | Status |
| --- | --- | --- | --- |
| Prompt injection / jailbreak | Input intervention blocks before retrieval or generation | Local call timeline plus synthetic AWS `prompt-attack-blocked`: `GUARDRAIL_INTERVENED`, `PROMPT_ATTACK=HIGH/BLOCKED` | PASS (local + AWS) |
| PII handling | Anonymized input is used for retrieval; high-risk PII is blocked | Local masking tests plus AWS `pii-anonymized`: `GUARDRAIL_INTERVENED`, `NAME`/`EMAIL` anonymized; `secret-pii-blocked`: `CREDIT_DEBIT_CARD_NUMBER/BLOCKED` | PASS (local + AWS) |
| Individualized advice / harmful content | Denied topic and harmful categories block before retrieval | Local advice/violence scenarios plus AWS `individualized-advice-blocked`: `GUARDRAIL_INTERVENED`, `IndividualizedLegalAdvice/BLOCKED` | PASS (local + AWS) |
| Contextual grounding | Query, authorized sources, and answer use required qualifiers; unsupported output blocks | Local payload tests plus AWS `grounded-output`: `NONE`, `GROUNDING=1.0`, `RELEVANCE=0.99`; `unsupported-output`: `GUARDRAIL_INTERVENED`, `GROUNDING=0.0/BLOCKED`, `RELEVANCE=0.3/BLOCKED` | PASS (local + AWS) |
| Authorization boundary | Cross-matter request is denied before every guardrail call | Cross-matter and unverified identity tests assert zero Guardrail calls | PASS (local) |
| Fail closed | Client errors, malformed actions/assessments, inconsistent action, and missing masked output stop processing | `tests/test_guardrails.py` covers parser and error cases | PASS (local) |
| Safe audit trail | Record correlation/stage/action/outcome without prompt, PII, evidence, or secrets | Audit sink and logging regression tests inspect emitted fields | PASS (local) |
| IaC and least privilege | Template contains only Guardrail + immutable version; runtime permission is scoped to that ARN | `tests/test_phase_06_guardrail_iac.py` checks resource types, policies, thresholds, and no wildcard IAM | PASS (static) |
| AWS smoke / thresholds | Representative real calls validate expected behavior and thresholds | Eight synthetic calls: safe input `NONE`; prompt attack blocked; PII anonymized; secret PII blocked; general legal information `NONE`; individualized advice blocked; grounded output `NONE`; unsupported output blocked | PASS (AWS) |

Guardrails are a probabilistic safety layer and do not provide authentication,
ownership, matter authorization, or retrieval filtering. The backend keeps
those decisions deterministic and upstream. AWS Guardrails invocations and
the two CloudFormation resources may incur charges. The smoke used eight short
requests and no model inference; using the published per-text-unit rates, the
Guardrails portion is expected to be below one cent for this small run, subject
to AWS pricing and actual text-unit rounding. The initial create attempt was
rejected because the denied-topic definition exceeded the default tier limit;
the definition was shortened to the supported limit, the stack was recreated,
and all smoke cases then passed. No resources remain deployed.
