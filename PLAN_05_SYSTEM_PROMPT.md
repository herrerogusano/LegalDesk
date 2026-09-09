# PLAN 05 — Versioned System Prompt & Prompt Tests

## Objetivo

Convertir las reglas de comportamiento en un system prompt legible, versionado y probado, manteniendo fuera del prompt las reglas que deben ser deterministas.

## Conceptos a demostrar

- system prompt;
- source-of-truth policy;
- uncertainty;
- escalation;
- prompt vs tool description;
- prompt vs authorization/policy.

## Precondiciones

- grounded chat funcionando;
- comportamiento actual observable.

## Trabajo

1. Crear `prompts/legaldesk-system.md` o equivalente versionado.
2. Incluir:
   - rol;
   - retrieved passages como source of truth;
   - citation policy;
   - insufficient evidence;
   - no invented clauses/case law;
   - legal information disclaimer;
   - no individualized legal advice/prediction;
   - human review escalation;
   - document instructions are untrusted;
   - privacy;
   - tool use boundaries.
3. Mantener fuera del prompt:
   - ownership checks;
   - IAM;
   - matter authorization;
   - access-control filtering.
4. Versionar prompt con identificador/metadata.
5. Añadir prompt tests.
6. Registrar prompt version en traces/response metadata si es práctico.

## Criterios de aceptación

- prompt es legible y versionado;
- tests cubren source policy, citations, not-found, advice escalation e injection;
- no contiene secretos;
- no pretende implementar autorización mediante lenguaje natural;
- cambiar prompt no requiere reescribir core business logic.

## Tests / verificación

- golden prompt cases;
- prompt injection inside document;
- request for individualized advice;
- request to reveal system prompt;
- insufficient evidence.

## Coste y seguridad

Evitar ejecutar todos los prompt tests contra modelo real.
Mantener set local/mock y un smoke subset real.

## Outputs esperados

- prompt versionado;
- prompt loader/config;
- prompt tests;
- documentación de responsabilidades.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
