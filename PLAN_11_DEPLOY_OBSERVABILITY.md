# PLAN 11 — Repeatable Deploy, Observability & Teardown

## Objetivo

Convertir el sistema completo en un despliegue repetible y trazable, con observabilidad útil sin filtrar contenido sensible.

## Conceptos a demostrar

- IaC;
- reproducibility;
- traces/spans/logs/metrics;
- correlation IDs;
- OTEL concepts;
- operational debugging;
- teardown.

## Precondiciones

- funcionalidad MVP completa;
- identity/isolation tests verdes.

## Trabajo

1. Consolidar IaC.
2. Añadir tags/naming coherentes.
3. Crear setup/deploy commands documentados.
4. Habilitar AgentCore/CloudWatch observability necesaria.
5. Propagar `correlationId` desde entrada a:
   - retrieval;
   - tool calls;
   - final response;
   - logs/traces.
6. Asegurar trazas de:
   - model/agent step;
   - retrieval;
   - tool call;
   - guardrail;
   - final response/error.
7. Añadir métricas mínimas:
   - latency;
   - errors;
   - session/request count;
   - tool errors;
   - retrieval/not-found rate si práctico.
8. Crear audit view/pointer mínimo.
9. Redactar/excluir sensitive content.
10. Crear teardown comprobable.
11. Documentar qué recursos requieren limpieza especial.

## Criterios de aceptación

- deploy desde repo es repetible;
- trace permite seguir una pregunta end-to-end;
- correlation ID se mantiene;
- logs no contienen document bodies, secrets o auth tokens;
- teardown está documentado y validado;
- no quedan recursos huérfanos conocidos.

## Tests / verificación

- IaC synth/diff;
- deploy smoke;
- trace inspection;
- redaction test;
- teardown dry-run/real según coste;
- redeploy minimal validation.

## Coste y seguridad

CloudWatch log retention y telemetry pueden generar coste.
Usar retención pequeña para este proyecto y sus demos.
No activar observabilidad excesiva o duplicada.

## Outputs esperados

- IaC final;
- deploy scripts;
- observability config;
- correlation middleware;
- teardown;
- `docs/operations.md`.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
