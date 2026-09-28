# LegalDesk — Master Plan

## Principio

Construimos un MVP seguro y explicable, no un producto legal real.

Cada fase agrega una capacidad y deja tests que protegen lo aprendido.

## Fases

| # | Plan | Resultado principal | Depende de |
|---|---|---|---|
| 00 | `PLAN_00_ARCHITECTURE.md` | threat model, data model, boundaries, repo bootstrap | — |
| 01 | `PLAN_01_AGENTCORE_RUNTIME.md` | agente mínimo en AgentCore Harness/Runtime | 00 |
| 02 | `PLAN_02_DOCUMENT_PIPELINE.md` | upload S3 + metadata DB | 00–01 |
| 03 | `PLAN_03_KNOWLEDGE_BASE.md` | ingest/retrieve con filtros | 02 |
| 04 | `PLAN_04_GROUNDED_CHAT.md` | chat con citations + not-found | 03 |
| 05 | `PLAN_05_SYSTEM_PROMPT.md` | prompt versionado + prompt tests | 04 |
| 06 | `PLAN_06_GUARDRAILS.md` | guardrails + tests adversariales | 05 |
| 07 | `PLAN_07_LAMBDA_TOOL.md` | `create_review_task` | 06 |
| 08 | `PLAN_08_GATEWAY_MCP.md` | Gateway + MCP metadata tools | 07 |
| 09 | `PLAN_09_MEMORY.md` | short-term + política long-term | 08 |
| 10 | `PLAN_10_IDENTITY_ISOLATION.md` | identity + cross-matter enforcement | 09 |
| 11 | `PLAN_11_DEPLOY_OBSERVABILITY.md` | IaC, traces, metrics, correlation IDs, teardown | 10 |
| 12 | `PLAN_12_EVALUATION_DEMO.md` | 20+ evals, README, demo, final audit | 11 |
| 13 | `PLAN_13_INTEGRATION_RELEASE.md` | integración E2E local y preparación de release | 00–12 |
| 14 | `PLAN_14_PUBLIC_BETA.md` | arquitectura y gates para beta pública autenticada | 13 |

## Milestones

### M1 — Agent skeleton
Fases 00–01.

Podemos invocar un agente mínimo y explicar Harness vs Runtime.

### M2 — Grounded RAG
Fases 02–04.

Podemos subir un documento, recuperar evidencia y responder con citas.

### M3 — Safe agent behavior
Fases 05–06.

Prompt y Guardrails están versionados y probados.

### M4 — Tool-using agent
Fases 07–08.

El agente usa Lambda y MCP a través de Gateway.

### M5 — Stateful, tenant-safe agent
Fases 09–10.

Memoria, identidad y aislamiento comprobados.

### M6 — Portfolio readiness (production gates pending)
Fases 11–14.

Fases 11–12 acreditan componentes, evaluación acotada y checklist de demo,
no una aplicación integrada. Fase 13 integra y verifica el recorrido local.
Fase 14 ha desplegado y probado la beta pública autenticada. La promoción a
`prod` sigue bloqueada hasta cerrar la evidencia semántica, los ejercicios de
operación/recuperación y las decisiones de propiedad, retención y coste.

## Reglas de avance

Una fase se considera terminada cuando:
- todos sus acceptance criteria están demostrados;
- tests relevantes pasan;
- no hay TODOs críticos ocultos;
- cualquier coste o recurso AWS está documentado;
- cualquier desviación arquitectónica está registrada;
- el supervisor produce cierre;
- el usuario acepta continuar.

## Estrategia de coste

Realizar primero todo lo posible localmente.

Despliegues/retrieval/inferencia reales:
- pequeños;
- deliberados;
- con dataset mínimo;
- nunca en loops innecesarios.

## Estrategia de seguridad

Aislamiento en capas:
1. identidad;
2. authorization mapping;
3. backend;
4. S3/data model;
5. retrieval metadata;
6. tool validation;
7. memory scoping;
8. tests adversariales.

No confiar en el LLM para ninguna decisión de acceso.

## Estado

Auditoría: `NOT_READY_FOR_PROD` (ver `docs/production-readiness.md`). Fases
00–13 conservan su aceptación acotada. La infraestructura de beta autenticada
de Fase 14 está desplegada y el smoke público pasó; la promoción a `prod`
permanece cerrada hasta completar los gates restantes.

- [x] Phase 13 integration, bounded live smoke and local release hardening (413 tests; 24/24 deterministic evaluations; local HTTP/browser E2E; final AWS browser smoke PASS through Cognito, upload/ingestion, RAG, direct Gateway→MCP/Lambda tools, cross-matter denial, audit and logout; verified teardown; fail-closed endpoint/retrieval hardening; independent holdout, production hosting/distributed state and prod promotion remain pending, NOT_READY_FOR_PROD)

- [ ] Phase 14 authenticated public beta architecture and production gates
  (`PLAN_14_PUBLIC_BETA.md`): planning tranche approved for CloudFront + private
  S3 frontend + API Gateway HTTP API + Lambda application, durable DynamoDB
  state, asynchronous ingestion, exact-origin security, and pre-provisioned
  authenticated users only; the local release candidate, bounded holdout
  runner, quarantine/reconciliation and operations controls are implemented
  and validated (562 tests, 24/24 deterministic evaluations, all 15 templates
  lint-clean). AWS deployment and the bounded browser smoke are complete;
  bounded rollback/forward-recovery, PITR restore/delete and reconciliation
  evidence are recorded. The final provider holdout remains 13/14 with
  `needs_follow_up`; the synthetic malware alarm was diagnosed, remediated and
  returned to `OK`. Strict custom-domain TLS is absent, owner decisions remain
  open, and prod promotion remains pending.

- [x] Phase 00
- [x] Phase 01
- [x] Phase 02
- [x] Phase 03 (initial local acceptance and approved AWS smoke complete with hierarchical chunking; current fixed-size configuration is validated locally only; smoke resources torn down)
- [x] Phase 04
- [x] Phase 05
- [x] Phase 06 (local acceptance and approved AWS Guardrails smoke complete; temporary stack deleted)
- [x] Phase 07 (local acceptance plus synthetic AWS deployment/smoke complete; Lambda retained for Phase 08+)
- [x] Phase 08 (Gateway, MCP, OAuth, Harness attachment, and synthetic live smoke complete)
- [x] Phase 09 (local acceptance and approved synthetic AWS Memory smoke complete)
- [x] Phase 10 (Cognito public PKCE client, Gateway artifact update, and synthetic identity-isolation smoke complete in eu-west-1; operational observability remains Phase 11)
- [x] Phase 11 (repeatable observability IaC, redacted application telemetry, metric filters, and synthetic AWS smoke complete in eu-west-1; managed ADOT detail disabled after content opt-out was ineffective, with bounded no-content smoke PASS and internal managed trace intentionally unavailable; teardown remains documented/dry-run)
- [x] Phase 12 (local deterministic evaluation/demo complete; resolver 1.1.0 passed 9/9, writer 1.2.0 passed 8/9 in the nine-call follow-up, and the sole remaining partial case passed its one-call hash-pinned follow-up; composite staged real-model subset 9/9, zero retries, metadata-only immutable reports, with production/statistical limits documented)
