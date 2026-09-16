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

### M6 — Portfolio-ready
Fases 11–12.

Deploy reproducible, observabilidad, evaluación, teardown y demo.

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

- [x] Phase 00
- [x] Phase 01
- [x] Phase 02
- [x] Phase 03 (initial local acceptance and approved AWS smoke complete with hierarchical chunking; current fixed-size configuration is validated locally only; smoke resources torn down)
- [x] Phase 04
- [x] Phase 05
- [ ] Phase 06
- [ ] Phase 07
- [ ] Phase 08
- [ ] Phase 09
- [ ] Phase 10
- [ ] Phase 11
- [ ] Phase 12
