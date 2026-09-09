# PLAN 08 — AgentCore Gateway + Remote MCP

## Objetivo

Exponer las herramientas requeridas a través de AgentCore Gateway: la Lambda de revisión y un servidor MCP remoto de metadata.

## Conceptos a demostrar

- MCP protocol;
- remote MCP server;
- Gateway;
- target;
- identity propagation;
- tool boundary;
- Gateway vs Lambda vs Runtime.

## Precondiciones

- `create_review_task` lista;
- document metadata repository estable;
- authorization context estable.

## Trabajo

1. Crear servidor MCP remoto pequeño.
2. Exponer:
   - `list_matter_documents`;
   - `get_document_metadata`.
3. Ambas tools deben ignorar cualquier intento del LLM de ampliar scope.
4. Conectar MCP remoto a AgentCore Gateway.
5. Conectar Lambda/API `create_review_task` como target vía Gateway.
6. Configurar auth/policies mínimas.
7. Configurar agent para descubrir/usar tools.
8. Asegurar schemas y descriptions concisos.
9. Probar tool selection:
   - listing → MCP;
   - metadata → MCP;
   - review → Lambda.
10. Registrar tool call/correlation ID.
11. Documentar diferencia:
   function / Gateway target / remote MCP.

## Criterios de aceptación

- una llamada del agente alcanza MCP vía Gateway;
- una llamada alcanza Lambda/API vía Gateway;
- MCP nunca lista/lee otro matter;
- metadata no revela secrets;
- Gateway tiene auth/config reproducible;
- tool descriptions no contienen lógica de autorización;
- tool traces son observables.

## Tests / verificación

- MCP unit tests;
- list current matter;
- get metadata current matter;
- invalid document ID;
- cross-matter denial;
- review tool via Gateway;
- tool selection tests.

## Coste y seguridad

Gateway/Runtime/Lambda invocations pueden generar coste.
MCP remoto debe usar hosting mínimo; preferir opción simple y barata compatible con el ejercicio.
Pedir aprobación antes de crear infraestructura facturable adicional.

## Outputs esperados

- MCP server;
- Gateway IaC/config;
- targets;
- agent tool integration;
- tests;
- `docs/gateway-mcp.md`.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
