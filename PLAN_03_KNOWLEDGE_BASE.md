# PLAN 03 — Bedrock Knowledge Base & Authorized Retrieval

## Objetivo

Convertir los documentos autorizados en una fuente RAG recuperable y demostrar retrieval con filtros por matter.

## Conceptos a demostrar

- RAG;
- chunking;
- embeddings;
- vector retrieval;
- metadata filtering;
- ingestion/sync;
- retrieval ≠ S3 access.

## Precondiciones

- documentos de Phase 02;
- metadata estable;
- dataset con al menos dos matters.

## Trabajo

1. Crear/configurar Bedrock Knowledge Base.
2. Conectar S3 como data source.
3. Elegir vector store/configuración mínima compatible.
4. Usar chunking fijo con `MaxTokens: 800` y `OverlapPercentage: 15`. El modo
   fijo mantiene un perfil de metadatos sencillo para S3 Vectors; el tamaño y
   solapamiento equilibran contexto, cantidad de chunks y coste de embeddings.
5. Adjuntar metadata necesaria para filtrar:
   `tenantId`, `matterId`, `documentId`, tipo y otros campos útiles.
6. Crear proceso de sync/ingestion deliberado, no continuo.
7. Implementar `search_legal_documents` como capa controlada:
   - recibe query + authorized context;
   - no acepta unrestricted tenant/matter;
   - aplica metadata filter;
   - normaliza passages + citations.
8. Propagar document/page/section cuando el servicio/source lo permita.
9. Manejar retrieval vacío.
10. Documentar exactamente qué recibe el modelo después; todavía no construir chat final.

## Criterios de aceptación

- retrieval de Matter A devuelve solo A;
- retrieval de Matter B devuelve solo B;
- existe caso con evidencia encontrada;
- existe caso sin evidencia;
- resultados contienen source metadata;
- nunca se da acceso libre del modelo al bucket;
- filtro de matter se construye server-side.

## Tests / verificación

- tests de filter builder;
- tests de result normalization;
- negative cross-matter retrieval;
- retrieval empty;
- una ingestión real mínima y consultas mínimas si se aprueba coste.

## Coste y seguridad

Knowledge Base, embeddings, vector storage, ingestion y retrieval pueden generar coste.
Antes de crear vector store/KB o ingerir, identificar recursos y pedir aprobación si procede.

No ejecutar reingestiones repetidas sin cambios.

## Outputs esperados

- IaC/config Knowledge Base;
- ingestion command/script;
- authorized retrieval service;
- normalized citation model;
- tests;
- `docs/rag.md`.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
