# PLAN 07 — Human Review Lambda Tool

## Objetivo

Crear una herramienta con side effect controlado: `create_review_task`, junto a
`list_review_tasks`, `get_review_task` y `update_review_task`, para una cola de
revisión humana purpose-specific sin emitir asesoramiento jurídico.

## Conceptos a demostrar

- agent tools;
- function contracts;
- side effects;
- human-in-the-loop;
- authorization at tool boundary;
- idempotency.

## Precondiciones

- authorization context disponible;
- Guardrails activos;
- DynamoDB/metadata layer existente.

## Trabajo

1. Definir schemas de creación, lectura y transición.
2. Implementar Lambda/service.
3. Derivar user/matter de context autorizado.
4. Validar input.
5. Añadir idempotency/correlation ID cuando sea práctico.
6. Persistir ReviewTask.
7. Retornar snapshot acotado + ID/estado, no advice ni documento completo.
8. Crear tool description precisa para el agente.
9. Integración inicial con agent/tool layer.
10. Preparar target para Gateway en Phase 08.

## Criterios de aceptación

- puede crear, listar, abrir y actualizar tasks del current matter;
- no puede leer ni actualizar otro matter;
- task contiene correlation metadata suficiente;
- tool no puede emitir advice;
- errores son seguros;
- least privilege IAM.

## Tests / verificación

- schema validation;
- authorized create;
- unauthorized matter;
- malformed input;
- idempotency si implementada;
- Lambda/service unit tests.

## Coste y seguridad

Lambda y DynamoDB pueden generar coste.
No desplegar/llamar repetidamente sin necesidad.
No enviar document body, S3 key ni source URI a la task; el snapshot es
server-derived y limitado a citas exactas.

## Outputs esperados

- Lambda;
- tool schema/description;
- persistence;
- tests;
- IaC target-ready.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
