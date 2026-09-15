# PLAN 02 — Document Upload & Metadata Pipeline

## Objetivo

Implementar la entrada de documentos: upload autorizado a S3 y metadata persistida por matter, todavía sin convertir el chatbot en RAG.

## Conceptos a demostrar

- object storage vs metadata;
- upload authorization;
- document lifecycle;
- S3 prefixes;
- processing states;
- defense in depth.

## Precondiciones

- Phase 01 verde;
- fixtures de matters;
- decision DynamoDB/S3 confirmada.

## Trabajo

1. Crear bucket/document storage mediante IaC.
2. Crear DynamoDB tables o single-table design mínimo para metadata.
3. Implementar endpoint/flow de upload.
4. Generar `documentId` server-side.
5. Derivar effective matter desde contexto autorizado.
6. Guardar metadata:
   type, jurisdiction, date, confidentiality, status.
7. Definir estados:
   `PENDING_UPLOAD`, `UPLOADED`, `PENDING_INGESTION`, `INDEXED`, `FAILED`.
   Metadata is created before a presigned PUT completes; confirmation checks
   the server-derived S3 key before transitioning `PENDING_UPLOAD` to
   `UPLOADED`.
   Lifecycle: `PENDING_UPLOAD → UPLOADED → PENDING_INGESTION → INDEXED`, with
   `FAILED` for failures at the applicable stage.
   Presigned authorization requires a declared file size within the limit;
   direct local uploads derive it from the body.
8. Aplicar allowed file types y límites razonables.
9. No permitir path/key arbitraria proporcionada por usuario.
10. Crear listing interno de documentos autorizado, que luego reutilizará MCP.
11. Añadir fixtures/documentos inventados.

## Criterios de aceptación

- User A puede subir a Matter A;
- User A no puede subir a Matter B;
- S3 key se deriva en servidor;
- metadata contiene tenant/matter/document IDs;
- no se almacena contenido bruto en DynamoDB;
- estado de procesamiento visible;
- infraestructura es reproducible.

## Tests / verificación

- unit tests de key generation;
- authorization tests;
- upload validation;
- metadata persistence con mocks/local;
- test negativo cross-matter;
- smoke test AWS opcional y mínimo.

Los uploads abandonados pueden permanecer en `PENDING_UPLOAD` por ahora. La
limpieza automática/TTL específico de objetos pendientes es un production gap
para una iteración posterior y no forma parte de esta fase.

## Coste y seguridad

S3 y DynamoDB pueden tener coste.
No crear recursos adicionales no necesarios.
Mantener documentos pequeños e inventados.

## Outputs esperados

- S3 IaC;
- DynamoDB IaC;
- upload API/service;
- document repository;
- tests;
- fixtures.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
