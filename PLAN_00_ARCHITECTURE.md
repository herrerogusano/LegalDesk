# PLAN 00 — Architecture, Threat Model & Repository Bootstrap

## Objetivo

Definir antes de programar las fronteras del sistema: entidades, flujo de datos, amenazas, ownership y estructura base del repo.

Esta fase no necesita un chatbot funcional ni recursos cloud de pago.

## Conceptos a demostrar

- agente vs aplicación tradicional;
- tenant y matter;
- authentication vs authorization;
- trust boundaries;
- threat modeling;
- source of truth;
- least privilege.

## Precondiciones

- leer `PROJECT_REQUIREMENTS.md`;
- leer `ARCHITECTURE_DECISIONS.md`;
- no existe dependencia de código previa.

## Trabajo

1. Crear estructura de proyecto para frontend, backend/agent, infra y tests.
2. Definir modelos `User`, `Matter`, `Document`, `Conversation`, `ReviewTask`.
3. Definir IDs y relaciones de ownership.
4. Dibujar el flujo:
   `Browser → Identity → AgentCore → KB/Guardrails/Gateway/Memory → AWS data stores`.
5. Crear threat model mínimo:
   - prompt injection;
   - cross-matter access;
   - forged tenant/matter identifiers;
   - poisoned document instructions;
   - PII leakage;
   - secret/log leakage;
   - unauthorized tool calls;
   - hallucinated legal answer.
6. Especificar qué datos nunca deben enviarse al modelo.
7. Crear dataset plan:
   - Tenant A / Matter A;
   - Tenant B / Matter B;
   - documentos inventados con cláusulas distinguibles.
8. Definir conventions de correlation IDs y audit metadata.
9. Preparar tests/fixtures básicos de authorization sin AWS.
10. Crear diagrama Mermaid inicial y mantenerlo versionado.

## Criterios de aceptación

- existe data model documentado;
- existe threat model;
- cada request tiene un diseño claro de cómo obtiene `userId`, `tenantId`, `matterId`;
- se documenta explícitamente que browser-provided tenant/matter IDs no son trusted;
- existen dos matters ficticios aptos para pruebas negativas;
- diagrama inicial cubre todas las cajas requeridas;
- estructura del repo está lista para las siguientes fases.

## Tests / verificación

- unit tests de ownership/membership;
- test donde User A intenta usar Matter B y el authorization layer devuelve deny;
- revisión manual del threat model frente a requisitos.

## Coste y seguridad

No crear recursos AWS de pago.
No usar documentos reales.
No almacenar secretos en fixtures.

## Outputs esperados

- estructura repo;
- modelos/schemas;
- `docs/architecture.md`;
- `docs/threat-model.md`;
- fixtures multi-tenant;
- tests de authorization base.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
