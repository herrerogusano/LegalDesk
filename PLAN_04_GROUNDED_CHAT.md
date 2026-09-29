# PLAN 04 — Grounded Chat & Citations

## Objetivo

Conectar retrieval con el agente para que las preguntas documentales se contesten desde pasajes autorizados y muestren citas.

## Conceptos a demostrar

- retrieve-then-generate;
- context construction;
- grounded answer;
- citation propagation;
- insufficient evidence;
- cross-document questions.

## Precondiciones

- Phase 03 retrieval estable;
- citation model normalizado.

## Trabajo

1. Definir request de chat con `conversationId`/`sessionId`.
2. Resolver authorized matter antes de recuperar.
3. Ejecutar retrieval antes de responder preguntas documentales.
4. Construir contexto sin incluir metadata/secrets innecesarios.
5. Exigir salida estructurada o parseable con:
   - answer;
   - citations;
   - evidence status;
   - disclaimer flag si procede.
6. Implementar respuesta canónica de insufficient evidence.
7. Preservar document name + page/section cuando exista.
8. Implementar citation panel mínimo en UI.
9. Añadir cross-document query en el mismo matter.
10. No permitir que una cita haga referencia a documentos no recuperados.

## Criterios de aceptación

- [x] pregunta answerable devuelve respuesta + cita correcta;
- [x] pregunta unanswerable devuelve not-found, no invención;
- [x] citations solo apuntan a retrieved passages;
- [x] cross-document funciona dentro del mismo matter;
- [x] cross-matter no contamina retrieval;
- [x] UI muestra citations en el panel local.

## Tests / verificación

- answerable;
- ambiguous;
- unanswerable;
- citation integrity;
- cross-document;
- cross-matter;
- mocked model tests cuando sea posible;
- pocas inferencias reales de smoke test.

## Coste y seguridad

Las inferencias y retrieval reales cuestan.
Usar mocks/fixtures para regression tests.
Real-model tests pequeños y explícitos.

## Outputs esperados

- chat orchestration;
- citation response schema;
- citation UI;
- grounded/not-found tests.

## Definition of Done

- [x] Código y documentación coherentes.
- [x] Tests relevantes verdes.
- [x] Acceptance criteria comprobados uno a uno.
- [x] Sin secretos ni datos legales reales.
- [x] Recursos AWS y posibles costes listados; no se crearon recursos.
- [x] No se ejecuta automáticamente la siguiente fase.
