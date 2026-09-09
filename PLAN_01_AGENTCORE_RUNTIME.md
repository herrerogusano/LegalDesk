# PLAN 01 — Minimal AgentCore Harness / Runtime

## Objetivo

Desplegar o preparar el agente mínimo en AgentCore, entendiendo qué aporta Harness y qué aporta Runtime.

Preferencia: AgentCore Harness para el MVP, salvo bloqueo técnico documentado.

## Conceptos a demostrar

- orchestration loop;
- Harness vs Runtime;
- session isolation;
- agent instructions/model/tools;
- AWS execution role;
- local dev vs managed runtime.

## Precondiciones

- Phase 00 completa;
- región configurada inicialmente como `eu-west-1`;
- credenciales AWS locales funcionando;
- `AWS_COST_POLICY.md` leído.

## Trabajo

1. Crear agente mínimo Python sin RAG ni tools de negocio.
2. Configurar un system instruction temporal muy corto.
3. Añadir endpoint/invocation local o dev.
4. Preparar AgentCore Harness mediante configuración oficial.
5. Mantener el IAM execution role mínimo.
6. Añadir config/versionado necesarios al repo.
7. Verificar session IDs distintos.
8. Documentar:
   - qué configura Harness;
   - qué ejecuta Runtime;
   - qué código sería nuestro si usáramos Runtime puro.
9. Añadir health/invoke test mínimo.
10. Preparar teardown del recurso creado.

## Criterios de aceptación

- el agente responde a una invocación mínima;
- existe una forma reproducible de desplegarlo;
- dos sesiones usan IDs diferentes;
- no hay dependencia todavía de S3/KB/Gateway/Memory;
- IAM no contiene permisos wildcard innecesarios;
- Harness vs Runtime está explicado en documentación.

## Tests / verificación

- test local del handler/entrypoint;
- smoke test de invocación;
- si se despliega, 1–2 invocaciones reales como máximo para validar;
- revisión del role policy.

## Coste y seguridad

AgentCore/model invocation puede generar coste.
Antes del primer deploy/invoke real, detenerse y pedir aprobación si el coste no fue aprobado explícitamente.

No realizar loops de inferencia.

## Outputs esperados

- código agente mínimo;
- configuración AgentCore;
- IaC/config;
- IAM mínimo;
- `docs/agentcore-harness-runtime.md`;
- teardown.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
