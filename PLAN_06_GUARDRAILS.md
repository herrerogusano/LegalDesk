# PLAN 06 — Bedrock Guardrails & Adversarial Tests

## Objetivo

Añadir Guardrails como capa de safety/grounding y demostrar sus límites frente a authorization.

## Conceptos a demostrar

- content filters;
- prompt attacks;
- PII filters;
- contextual grounding;
- denied topics;
- guardrail != authorization.

## Precondiciones

- system prompt estable;
- grounded chat estable.

## Trabajo

1. Crear Guardrail mediante IaC/config.
2. Configurar según disponibilidad:
   - prompt attack protection;
   - sensitive information/PII handling;
   - denied topics relacionados con individualized legal advice donde sea adecuado;
   - harmful/abusive content;
   - contextual grounding checks.
3. Aplicarlo en los puntos relevantes de input/output.
4. Definir respuestas de bloqueo claras.
5. Añadir tests expected-vs-actual.
6. Demostrar que un cross-matter request se bloquea **antes** por authorization aunque Guardrail no lo detecte.
7. Registrar guardrail outcome sin datos sensibles.
8. Mantener thresholds/config documentados.

## Criterios de aceptación

- injection test tratado correctamente;
- PII test bloquea/máscara según política;
- legal-advice test rechaza/escala según diseño;
- grounding test detecta salida no supported cuando aplique;
- cross-matter depende de auth, no Guardrail;
- resultados de tests quedan documentados.

## Tests / verificación

- prompt injection;
- jailbreak;
- PII;
- abusive content;
- ungrounded answer;
- individualized legal advice;
- cross-matter bypass attempt.

## Coste y seguridad

Guardrail invocations pueden tener coste según configuración/uso.
No ejecutar suites adversariales grandes repetidamente contra AWS.
Usar subset representativo para validación real.

## Outputs esperados

- Guardrail IaC/config;
- integration layer;
- adversarial tests;
- expected-vs-actual report;
- docs.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
