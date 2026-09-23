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

## ADR-016 — Deterministic explicit tools through Gateway

Las acciones explícitas de la UI `get_document_metadata`,
`list_matter_documents` y las operaciones `create_review_task`,
`list_review_tasks`, `get_review_task` y `update_review_task` se envían desde el backend a
AgentCore Gateway mediante un único `POST tools/call` MCP. No se delegan en la
selección de herramientas de Harness: `allowedTools` limita la selección del
modelo pero no obliga a que el modelo invoque una herramienta. Harness queda
reservado para flujos genuinamente agentic en los que la decisión del modelo
sea parte del producto.

La llamada directa reutiliza el `HarnessInvocationBinding` sellado existente:
JWT verificado, contexto de matter reconstruido en servidor, grant corto,
correlación y allowlist. El interceptor Gateway vuelve a autenticar y
autorizar antes de transformar la petición; MCP/Lambda vuelve a comprobar el
grant. La ruta falla cerrada, no sigue redirecciones, no reintenta, limita la
respuesta y reserva el contador `gateway` del presupuesto de smoke.

Esta decisión corrige la evidencia del tercer smoke: la UI alcanzó el backend
pero Harness terminó sin un tool call estructurado, por lo que no hubo grant,
interceptor ni Lambda. No añade infraestructura ni sustituye las
comprobaciones de autorización existentes.

## ADR-017 — Cola de revisión durable y purpose-specific

La revisión humana deja de ser únicamente metadata de una intención y pasa a
ser una cola durable purpose-specific. El backend obtiene la última respuesta
aceptada de la conversación/correlación vigente y construye un snapshot mínimo
(pregunta, respuesta, estado de evidencia, hashes de prompt disponibles y
citas exactas acotadas); el navegador no puede aportar esos campos ni estado,
autoría o timestamps. Crear, listar, abrir y actualizar pasan por AgentCore
Gateway hacia la misma Review Lambda y la tabla DynamoDB existente. La Lambda
revalida el grant, matter y transición (`OPEN → IN_REVIEW → CLOSED`, o cierre
directo), y cerrar exige una nota de resolución.

La demo Phase 13 mantiene el candidato aceptado en memoria y documenta ese
límite de despliegue distribuido; la task creada sí es durable. Esta cola no es
AgentCore long-term Memory. No se asignan personas ni se emiten notificaciones
automáticas, y la política posterior al cierre queda como gap de producción
explícito.

## ADR-018 — Beta pública autenticada y topología de hosting

**Decisión aprobada para la planificación de Phase 14:** publicar únicamente
una beta autenticada para documentos ficticios o públicos, sin signup anónimo ni
alta self-service de tenants/memberships. Los usuarios Cognito y sus
pertenencias a matters se provisionan y autorizan en backend antes del uso.

La topología objetivo es:

`Browser HTTPS → CloudFront (ACM, edge limits) → private S3 frontend (OAC)`

con `/callback` y `/api/*` dirigidos por CloudFront a un **API Gateway HTTP
API → Lambda application adapter**. El backend conserva los tokens OAuth
server-side, valida issuer/scope/expiry y mantiene las decisiones de
autorización deterministas existentes. S3 de documentos sigue privado y solo
emite URLs PUT prefirmadas de corta duración.

La aplicación deja de depender de estado de proceso. Como primera opción,
sessions, OAuth state, citation handles, conversation correlations, accepted
history/review candidates y redacted audit records reutilizan la tabla
DynamoDB de metadata mediante prefijos de entidad explícitos, escrituras
condicionales, proyecciones acotadas y permisos IAM por recurso/prefijo. Una
tabla separada sigue siendo una alternativa si la revisión de seguridad exige
aislar tokens o pasajes; requiere una decisión y coste documentados. La
expiración se comprueba en aplicación; TTL solo ayuda a limpiar y no es una
decisión de autorización.

La ingesta se convierte en asíncrona: el request inicia un job acotado y el
cliente observa un status separado. Ningún request público espera el polling
de Bedrock. La reconciliación de uploads abandonados, ingestas atascadas y
grants expirados es un proceso bounded y autorizado; no se usan scans
ilimitados ni TTL como garantía de borrado.

La seguridad de origen es exacta: hostname HTTPS único aprobado, cookies
`Secure; HttpOnly; SameSite=Lax`, CSP con hosts de upload explícitos, HSTS en
la edge, CORS S3 exacto, límites de body/rate y sin S3 público. WAF es una
opción separada de coste y aprobación; no sustituye authorization backend.

Esta decisión aprueba el diseño y la implementación local, pero no crea
recursos ni autoriza AWS, inferencia, datos legales reales o promoción
`developer → prod`. Esas acciones requieren
los gates y el envelope de coste de `PLAN_14_PUBLIC_BETA.md` y
`docs/phase-14-cost-operations.md`.
