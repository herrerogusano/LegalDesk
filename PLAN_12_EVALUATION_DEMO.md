# PLAN 12 — Evaluation, Security Evidence, README & Demo

## Objetivo

Demostrar que LegalDesk cumple el brief mediante una suite repetible de evaluación, documentación de portfolio y una demo corta.

## Conceptos a demostrar

- eval dataset;
- quality metrics;
- security evidence;
- regression;
- groundedness;
- production gaps;
- communicating architecture.

## Precondiciones

- todo el MVP desplegable;
- traces disponibles;
- teardown disponible.

## Trabajo

1. Crear dataset de >=20 casos.
2. Cubrir categorías:
   - answerable;
   - ambiguous;
   - unanswerable;
   - cross-document;
   - malicious;
   - PII;
   - cross-matter;
   - advice/escalation.
3. Definir scoring:
   - citations;
   - groundedness;
   - refusal/escalation;
   - access control;
   - tool choice;
   - latency.
4. Implementar runner local/automatizado.
5. Minimizar llamadas reales mediante fixtures/mocks y un subset real.
6. Guardar expected vs actual sin datos sensibles.
7. Crear arquitectura final.
8. Completar README:
   - setup;
   - architecture;
   - trade-offs;
   - limitations;
   - security assumptions;
   - production gaps;
   - cost/teardown.
9. Crear walkthrough de 5 minutos:
   `upload → ask → cite → MCP → review task → trace`.
10. Escribir:
   `What I would change before real legal data entered this system`.
11. Revisar todas las preguntas de entrevista del ejercicio.
12. Verificar acceptance criteria global.

## Criterios de aceptación

- >=20 evals definidas;
- cross-matter tests 100% deny;
- answerable cases muestran citations;
- unanswerable no inventa;
- malicious/PII/advice tienen outcome esperado;
- demo cubre el circuito requerido;
- README permite a otra persona entender y desplegar;
- teardown presente;
- reflexión production-ready presente.

## Tests / verificación

- runner completo;
- security regression suite;
- smoke real subset;
- manual demo checklist;
- acceptance matrix global.

## Coste y seguridad

Evaluaciones con modelo real pueden consumir bastante.
No correr la suite completa repetidamente en cloud.
Separar deterministic/local eval de real-model smoke eval.

Destruir infraestructura al terminar cuando no se necesite.

## Outputs esperados

- eval suite;
- results template/report;
- architecture diagram;
- final README;
- demo script/checklist;
- production-gaps write-up.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.


## Acceptance criteria globales a confirmar

- [ ] Auth + select matter + upload + ask
- [ ] Grounded answer + citations
- [ ] Lambda/API tool through Gateway
- [ ] Remote MCP tool through Gateway
- [ ] Guardrails + automated tests
- [ ] Versioned system prompt
- [ ] Conversation context
- [ ] Safe long-term memory or justified rejection
- [ ] Cross-matter isolation
- [ ] Traces for decision/retrieval/tool/guardrail/final
- [ ] Repeatable deploy
- [ ] Teardown
- [ ] README trade-offs/limitations/security/production gaps
