# LegalDesk — Architecture Decisions

Este archivo contiene decisiones iniciales. Si una fase demuestra que alguna debe cambiar, actualizar este archivo y documentar el motivo.

## ADR-001 — Región

**Decisión inicial:** `eu-west-1` (Europe/Ireland).

Motivos:
- continuidad con proyectos AWS anteriores;
- AgentCore Harness está soportado en la región;
- reduce cambio de contexto operativo.

Antes de desplegar un recurso, verificar que **esa capacidad concreta** y el modelo elegido estén disponibles en la región.

## ADR-002 — Lenguaje backend/agente

**Python 3.x**.

Motivos:
- continuidad con ejercicios AWS anteriores;
- buen soporte SDK AWS/AgentCore;
- adecuado para agentic/RAG workflows.

## ADR-003 — AgentCore Harness vs Runtime

**Preferencia MVP: AgentCore Harness.**

Modelo mental:
- Harness = loop/orquestación de agente gestionado por configuración.
- Runtime = entorno serverless seguro donde puede ejecutarse el agente; con Runtime puro mantenemos más código/orquestación.

Harness es una abstracción gestionada que se ejecuta sobre Runtime.

Usar Harness mientras permita cumplir los requisitos sin ocultar conceptos esenciales.
Cambiar a Runtime custom solo si una necesidad técnica real lo exige.

## ADR-004 — Infrastructure as Code

Todo recurso persistente del proyecto debe ser reproducible con IaC o con configuración versionada generada por las herramientas oficiales.

Preferencia:
- AgentCore CLI/CDK para recursos AgentCore;
- CDK para infraestructura complementaria si simplifica una única pila coherente.

Evitar mezclar múltiples herramientas IaC sin necesidad.

## ADR-005 — Base de datos

**DynamoDB** como opción inicial para metadata, authorization mappings y review tasks.

No almacenar cuerpos completos de documentos en DynamoDB.

## ADR-006 — S3

S3 almacena originales de documentos de prueba.

Diseñar keys con separación explícita, por ejemplo:

`tenants/{tenantId}/matters/{matterId}/documents/{documentId}/...`

La key no sustituye autorización; es defensa en profundidad.

## ADR-007 — Retrieval

El modelo no recibe acceso libre a S3.

Flujo:

`S3 document → ingestion/indexing → Knowledge Base → metadata-filtered retrieval → retrieved passages → model`

Para preguntas documentales, recuperar antes de responder.

Implementación Phase 03: Amazon Bedrock Knowledge Base con S3 como data source,
S3 Vectors como vector store, Amazon Titan Text Embeddings V2 en 1024
dimensiones y chunking fijo (`MaxTokens: 800`, `OverlapPercentage: 15`). Esta
configuración sustituye el chunking jerárquico inicial, cuyo smoke histórico no
valida los parámetros actuales. Se elige el modo fijo para mantener el perfil
de metadatos más sencillo y compatible con el presupuesto de metadatos de S3
Vectors; no se afirma que reduzca el coste total, ya que el tamaño y solapamiento
también afectan al número de chunks y embeddings.
El índice de S3 Vectors reserva `AMAZON_BEDROCK_TEXT` y
`AMAZON_BEDROCK_METADATA` como metadata no filterable; los atributos propios
de LegalDesk, incluidos `tenantId` y `matterId`, permanecen filterable.
`search_legal_documents` deriva el filtro
AND `tenantId`/`matterId` desde `RequestContext` en el backend y vuelve a
comprobar el scope de cada resultado. El modelo solo podrá recibir los
passages/citations autorizados; no recibe credenciales ni acceso directo a S3 o
a la Knowledge Base.

## ADR-008 — Identidad

Preferencia MVP: Amazon Cognito, salvo que una integración OIDC existente resulte claramente más simple.

`tenantId` y `matterId` efectivos deben salir de:
- identidad verificada;
- membership/authorization guardada en backend;
- contexto de request construido en servidor.

Nunca confiar en un valor libre enviado por frontend.

## ADR-009 — Gateway

AgentCore Gateway será la frontera de acceso a tools requeridas por el ejercicio.

Objetivos:
- exponer Lambda/API tool;
- conectar MCP remoto;
- centralizar identidad/observabilidad/política donde aplique.

## ADR-010 — Guardrails

Guardrails protege frente a categorías de contenido, PII, prompt attacks y respuestas no grounded según configuración.

**No es autorización.**

Un request prohibido por ownership/matter debe ser rechazado por controles deterministas aunque el modelo o Guardrail no detecten nada.

## ADR-011 — Memory

Short-term memory: habilitada.

Long-term memory: empezar **deshabilitada o extremadamente restringida** hasta que exista una allowlist clara de información inocua.

Nunca persistir texto legal bruto o conclusiones legales como memoria larga.

Phase 09 concreta esta decisión con AgentCore Memory sin `MemoryStrategies`,
eventos con `EventExpiryDuration` de siete días y `extractionMode=SKIP`. La
política de aplicación rechaza cualquier escritura o retrieval long-term,
incluidas preferencias inocuas, hasta una futura revisión de clasificación de
datos. `actorId` y `sessionId` se derivan de `RequestContext` autorizado y de
selectores de conversación; no se aceptan como valores libres del navegador o
CLI.

## ADR-012 — Observabilidad

Cada request tendrá un `correlationId`.

Trazas deben permitir reconstruir:
- request;
- sesión;
- retrieval;
- decisión/tool call;
- resultado de guardrail;
- respuesta final;
- errores/latencia.

Redactar o excluir:
- cuerpos completos de documentos;
- tokens;
- secretos;
- PII innecesaria.

## ADR-013 — Frontend

Mantener UI mínima.

Debe demostrar:
- login;
- matter selector;
- upload;
- chat;
- citations panel;
- metadata/review task;
- audit/trace pointer.

No invertir tiempo temprano en diseño visual.

## ADR-014 — Dataset

Solo documentos públicos o inventados.

Crear al menos dos tenants/matters ficticios con datos claramente diferenciables para probar aislamiento.

## ADR-015 — Stretch goals

No iniciar ningún stretch goal hasta completar `PLAN_12`.

Después se pueden valorar:
- comparación de versiones;
- reviewer UI;
- Agent Registry;
- dashboard de evaluación;
- skills;
- borrado + reingestión + memory cleanup.
