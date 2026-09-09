# PLAN 10 — Identity, Authorization & Cross-Matter Isolation

## Objetivo

Completar el sistema de identidad y demostrar que un usuario/matter nunca puede recuperar, citar, recordar o accionar datos de otro matter.

## Conceptos a demostrar

- Cognito/OIDC;
- verified claims;
- authentication vs authorization;
- tenant/matter membership;
- identity propagation;
- defense in depth;
- negative security testing.

## Precondiciones

- todas las capas funcionales existen;
- fixtures Tenant A/B;
- memory y tools disponibles.

## Trabajo

1. Implementar/terminar Cognito u OIDC.
2. Validar tokens server-side.
3. Extraer identity claims.
4. Resolver membership y authorized matters desde backend/DB.
5. Crear request security context inmutable.
6. Revisar todos los puntos:
   - upload;
   - document list;
   - KB retrieval;
   - citations;
   - MCP;
   - Lambda;
   - memory;
   - audit view.
7. Eliminar cualquier trust en tenant/matter libre del browser.
8. Aplicar least privilege IAM/resource policies.
9. Añadir comprehensive cross-matter attack tests.
10. Si Runtime está detrás de Gateway y el diseño lo requiere, evaluar impedir bypass directo.
11. Documentar matriz de autorización.

## Criterios de aceptación

Un User de A no puede:
- ver metadata de B;
- recuperar passages de B;
- obtener citas de B;
- llamar tools sobre B;
- crear ReviewTask en B;
- leer memoria de B;
- forzar acceso cambiando IDs en request.

Todos los denials son deterministas y no dependen de LLM/Guardrail.

## Tests / verificación

- forged matterId;
- forged tenantId;
- guessed documentId;
- MCP cross-matter;
- KB cross-matter;
- memory cross-actor;
- review task cross-matter;
- direct endpoint/bypass test cuando aplique.

## Coste y seguridad

No aumentar complejidad enterprise sin necesidad.
No almacenar tokens en logs.
Mantener claims mínimos.

## Outputs esperados

- identity integration;
- authorization service;
- matrix;
- negative security suite;
- IAM review.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
