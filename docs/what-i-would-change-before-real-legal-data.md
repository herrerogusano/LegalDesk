# What I would change before real legal data entered this system

1. Obtain a formal threat model and privacy/security review covering tenant
   isolation, identity claims, retention, deletion, exports, and incident
   response.
2. Replace fictional authorization fixtures with production-reviewed Cognito or
   OIDC configuration, key rotation tests, membership lifecycle controls, and
   break-glass procedures.
3. Define a documented data classification and redaction policy for documents,
   prompts, memory, citations, CloudWatch, provider traces, and support access.
4. Add an approved ingestion queue, malware/content validation, abandoned-upload
   cleanup, reprocessing strategy, and immutable document/version audit trail.
5. Validate retrieval quality and citation integrity with a legally reviewed,
   consented benchmark; keep model evaluation separate from authorization tests.
6. Revisit long-term memory only after an allowlist, retention period, deletion
   contract, and user-visible controls are approved.
7. Add operational SLOs, alarms, cost budgets, regional recovery, dependency
   failure drills, and a verified teardown/runbook for every retained resource.
8. Run the explicitly approved real-model subset with payload logging disabled,
   bounded calls, synthetic first, and an independent review of outputs before
   any production-like data is considered.

The current project is a portfolio MVP. Its local evaluation demonstrates
deterministic boundaries and contract behavior; it is not a certification for
legal use.
