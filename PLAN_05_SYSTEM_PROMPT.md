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

1. [x] Crear `prompts/legaldesk-system.md` o equivalente versionado.
2. [x] Incluir:
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
3. [x] Mantener fuera del prompt:
   - ownership checks;
   - IAM;
   - matter authorization;
   - access-control filtering.
4. [x] Versionar prompt con identificador/metadata.
5. [x] Añadir prompt tests.
6. [x] Registrar prompt version en response metadata con hash de artefacto.
7. [x] Definir en el prompt la salida JSON que valida el backend y comprobar que campos/estados siguen alineados.

## Criterios de aceptación

- [x] prompt es legible y versionado;
- [x] tests cubren source policy, citations, not-found, advice escalation e injection;
- [x] contrato JSON del prompt coincide con los campos y estados del backend;
- [x] no contiene secretos;
- [x] no pretende implementar autorización mediante lenguaje natural;
- [x] cambiar prompt no requiere reescribir core business logic.

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

- [x] Código y documentación coherentes.
- [x] Tests relevantes verdes.
- [x] Acceptance criteria comprobados uno a uno.
- [x] Sin secretos ni datos legales reales.
- [x] Recursos AWS y posibles costes listados.
- [x] No se ejecuta automáticamente la siguiente fase.
