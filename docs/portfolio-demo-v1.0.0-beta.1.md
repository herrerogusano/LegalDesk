# LegalDesk — portfolio demo script (`v1.0.0-beta.1`)

This is a short, evidence-led demo of the constrained beta. Use only the
pre-provisioned demo identity and the fictional fixtures already in the
repository. Do not enter passwords, tokens, client names or real legal
documents on screen. The demo does not imply legal advice, broad legal
accuracy, anonymous signup, strict TLS or support for real legal data.

## Before starting

- Confirm the release is `CONSTRAINED_PROD_BETA_DEPLOYED` and use the recorded
  release evidence, or run the loopback walkthrough described in
  [`phase-13-manual-demo.md`](phase-13-manual-demo.md).
- Keep these fictional fixtures available: `tests/fixtures/manual-demo-evidence.txt`
  and, when demonstrating isolation, the two fixtures described in
  [`dataset-plan.md`](dataset-plan.md).
- Have no secret or document body visible in a terminal, screen share or
  captured evidence. Record only bounded IDs, statuses and counts.

## Seven-minute walkthrough

1. **Login (0:00–0:45).** Sign in with a pre-provisioned demo user. Point out
   that there is no anonymous signup and that the browser receives selectors,
   not authority. Select the authorized demo matter.
2. **Upload and indexing (0:45–1:45).** Upload the fictional evidence file and
   show the lifecycle `PENDING_UPLOAD → UPLOADED → PENDING_INGESTION → INDEXED`
   (or the explicit `FAILED` path). Wait for **Listo para consultar** before
   asking a question; do not bypass the quarantine/content-validation step.
3. **Grounded RAG and citations (1:45–2:45).** Ask:
   `What is the inspection period?` Show the answer **four years**, then open
   the citation and verify the inspected passage is exactly the fictional
   source passage. Explain that retrieval is filtered and citations are
   rechecked server-side.
4. **Insufficient evidence (2:45–3:15).** Ask:
   `none: where is the missing emergency assembly point?` Show
   `insufficient_evidence` and no citation. Explain that the assistant does not
   invent an answer when the authorized corpus does not support it.
5. **Human review (3:15–4:15).** Save the question/answer for review, show the
   pending task and due date, then optionally move it through review and close
   it with a resolution note. Emphasize that the queue is a bounded workflow;
   no reviewer is assigned or notified automatically by this demo.
6. **Cross-matter isolation (4:15–5:15).** With the same actor, attempt the
   separately recorded unauthorized matter check. Expect a fail-closed `403`
   (or an equivalent denied UI state), no foreign document metadata and no
   retrieval. Do not use a real tenant or modify authorization data for the
   demonstration. The two-matter fixture and automated security evidence are
   the authoritative proof when the UI exposes only one authorized matter.
7. **Audit and logout (5:15–7:00).** Open technical audit and show the bounded
   application events actually present, such as conversation, chat, metadata
   tool and review operations. Explain that Cognito login, upload lifecycle and
   edge denials may be evidenced by their own redacted service telemetry rather
   than this application audit view. Confirm that prompts, answers, document
   bodies, tokens and storage URLs are absent. Sign out and show the session is
   no longer usable.

## Closing message

The design separates identity, server-side authorization, filtered retrieval,
grounding/citations, review workflow and redacted audit. The beta is therefore
demonstrable and constrained, but it remains limited to pre-provisioned users
and fictional/public documents. A custom domain/strict TLS, anonymous signup
and admission of real legal data are deliberately outside this release.
