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
- continuidad con proyectos AWS anteriores;
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

AgentCore Gateway será la frontera de acceso a tools requeridas por el proyecto.

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

## ADR-019 — Malware gate before public indexing

En la beta pública un objeto recién subido permanece en `PENDING_UPLOAD` y no
puede ser seleccionado para ingesta hasta que el resultado de GuardDuty Malware
Protection sea `NO_THREATS_FOUND`. En public mode, el PUT usa físicamente
`quarantine/tenants/...`, fuera del prefijo `tenants/` de la data source; así un
job Bedrock de otro documento no puede ingerir un upload pendiente. El handler
solo acepta esa quarantine key, copia server-side al key canónico bajo
`tenants/`, escribe allí el sidecar y elimina la quarantine después de la
transición condicional. El modo loopback conserva el key canónico por
compatibilidad. El handler local/production-safe acepta solo
el evento EventBridge exacto (`source=aws.guardduty`, detail-type
`GuardDuty Malware Protection Object Scan Result`, account, región, bucket y
key configurados). Además valida el esquema oficial: `detail.scanStatus` es
`COMPLETED`, `SKIPPED` o `FAILED`, y el veredicto se obtiene de
`detail.scanResultDetails.scanResultStatus`, con combinaciones coherentes y
razones acotadas. Resuelve tenant/matter/document desde metadata server-side,
y vuelve a comprobar `HEAD` de S3, tamaño, Content-Type, metadata, tag
`GuardDutyMalwareScanStatus` y ETag/version cuando están disponibles. Un
evento limpio para un objeto cambiado no promociona el documento.

`THREATS_FOUND`, `UNSUPPORTED`, `ACCESS_DENIED` y `FAILED` son terminales: el
objeto y su sidecar se eliminan según la política de beta y el registro queda
en `FAILED`; errores de corroboración o eventos forjados no mutan metadata.
La promoción y el fallo terminal usan una operación de metadata atómica y
condicional (`PENDING_UPLOAD + PENDING` como precondición); una carrera pierde
cerrada y no puede resucitar ni sobreescribir una transición de reconciliación.
Resultados repetidos son idempotentes y un resultado limpio fuera de orden no
puede resucitar un documento terminal. Si la limpieza posterior a una
transición falla, el documento queda no indexable y un evento repetido reintenta
la limpieza. La composición pública activa el gate;
el modo loopback conserva fixtures locales y no representa una autorización de
despliegue. La integración EventBridge/IAM/GuardDuty y cualquier cuarentena
durable siguen siendo gates de infraestructura separados, sin llamadas AWS en
esta fase.

## ADR-020 — Independent IDP and purpose-specific machine review creation

Approved by the owner on 2026-10-09 after the mandatory preflight STOP in
`docs/idp-preflight.md`; authoritative scope is `PHASE_14_IDP_PLAN.md`.
This is an extension of the existing LegalDesk application, not another project.

IDP processing has its own lifecycle and immutable extraction/correction
history linked to the existing authorized document. Registry-driven schemas
and backend-allowlisted deterministic rules distinguish literal, derived and
provisional values. Technical evidence checks never certify legal meaning.
Existing upload, malware, indexing and RAG boundaries remain unchanged.

Use asynchronous SQS/Lambda processing and conditional durable claims, with
Textract SNS/SQS continuation only when page-aware digital extraction cannot
cover the PDF. Enqueue only from verified clean promotion; durable dispatch
recovery must handle the DynamoDB-to-SQS gap. No Step Functions, unconditional
paid-call retry or queue-derived tenant ownership. Reuse the existing composite
DynamoDB key space for logically distinct runs/corrections, with bounded Query
and private immutable S3 artifacts outside RAG inclusion prefixes.

Automatic review creation uses a dedicated short-lived Cognito M2M identity
through the existing AgentCore Gateway. A dedicated machine scope/client is
not a human identity, membership, session or permission to use general tools.
The interceptor must authenticate the configured machine client/scope, resolve
an expected durable run and corroborate document/tenant/matter/content scope,
then issue a short-lived purpose-bound grant. The Review target repeats those
checks, accepts only references to server-held IDP fields/evidence and records
service-actor provenance and correlation. Reuse review business logic through
an additive, versioned IDP snapshot adapter; never manufacture an accepted chat
answer to satisfy ADR-017. Replays must not duplicate review tasks.

Machine calls cannot approve, correct, reject or close reviews, select arbitrary
matters or enter the existing human-tool branch. Human field correction remains
JWT/matter-authorized with immutable reviewer/time/old/new/evidence/reason audit.
Applicable human-confirmed values prevail over later unreviewed extraction.
ADR-016's mandatory Gateway boundary and existing human grants remain intact;
direct Review Lambda invocation or stored human JWTs are forbidden.

Selected-document structured queries may return IDP fields with explicit origin
and acceptance labels, otherwise use existing authorized RAG/citation behavior.
RAG responses must not silently become verified IDP facts. Existing upload cap
remains 10 MiB; a configurable 20 MB IDP engine ceiling does not widen it.

Rollout is disabled-by-default until infrastructure, IAM, bounded cost envelope
and real smoke gates are reviewed. Existing code-only CD credentials must not
gain infrastructure/IAM mutation privileges. M2M token requests, OCR, Bedrock,
queue/compute/storage and monitoring usage are billable classes, not free.
This ADR records approved design, not deployment or IDP acceptance evidence.

## ADR-021 — Chat agentic de solo lectura con AgentCore Harness

Estado: aprobado por el propietario el 2026-11-01. Complementa ADR-016 (las
acciones explícitas de la UI siguen siendo deterministas) y ADR-011 (memoria).
Plan de ejecución: `PLAN_15_AGENTIC_CHAT.md`.

### Contexto

- ADR-016 sacó del agente las acciones explícitas de la UI: un botón ya expresa
  la intención y Harness no puede forzar un tool call (`allowedTools` es una
  allowlist; `InvokeHarness` no tiene `toolChoice`). Esa decisión se mantiene.
- Desde entonces ningún LLM decide nada en LegalDesk. El chat tiene dos caminos
  que elige el usuario en la UI: RAG directo (sin Gateway) o un campo IDP
  seleccionado (Gateway → MCP `get_document_metadata`). Harness está desplegado
  pero no se usa.
- ADR-016 reservó Harness para "flujos genuinamente agentic en los que la
  decisión del modelo sea parte del producto". Una pregunta libre en el chat lo
  es: elegir la fuente (texto, campo extraído, inventario) es justo la decisión
  que el usuario no debería tener que tomar.

### Decisión

#### D1. Alcance

La caja de chat la gestiona un agente de Harness, que decide qué tools usar.
Los botones, la cola de revisión y el pipeline IDP no cambian (ADR-016 y
ADR-020 siguen vigentes). Si el usuario ya ha seleccionado documento y campo,
se mantiene el atajo determinista actual y no se invoca al agente: es la misma
lógica de ADR-016, porque la intención ya está expresada.

#### D2. El agente solo lee

Tools visibles para el agente, todas por Gateway:

| Tool | Target | Estado |
|---|---|---|
| `search_documents` | **target Lambda propio** `search-documents` | nueva |
| `list_matter_documents` | MCP `metadata-mcp` | existente |
| `get_extracted_fields` | MCP `metadata-mcp` | nueva, en lote (máximo 20 documentos por llamada; solo campos del registro IDP) |

- `search_documents` ejecuta el pipeline grounded actual (KB Retrieve →
  evidence resolver → answer writer → Guardrail → citas validadas) y devuelve
  la respuesta estructurada. Tiene su propio target para que el IAM de KB,
  modelo y Guardrail no se añada a la Lambda MCP.
- Las tools de review no son visibles para el agente y **el chat agentic no
  propone revisiones** (decisión del propietario, 2026-11-02). La cola de
  revisión de respuestas no cierra el círculo: no corrige datos ni devuelve la
  respuesta del revisor a la conversación, y en la beta no hay un rol de
  revisor distinto del usuario. El agente no la promueve; qué hacer con ella
  (darle sentido con roles y respuesta de vuelta, dejarla como pieza didáctica
  o retirarla) queda como decisión de producto aparte.
- **Verificación de datos**: cada línea de un bloque de datos lleva
  `{documentId, fieldName}`. La UI enlaza a la revisión IDP si el campo está
  pendiente de revisión, o al panel de datos extraídos del documento (valor,
  página y cita) si está aceptado. No se crea un flujo nuevo de corrección.
  Corregir en origen un campo aceptado e incorrecto requeriría que el usuario
  pudiera abrir revisiones IDP, que hoy solo crea el pipeline (ADR-020). Queda
  registrado como mejora aparte, con su propio ADR.
- **Citas inspeccionables**: `search_documents` devuelve sus citas en el
  resultado y el worker, que conoce sujeto, matter y conversación, crea los
  handles de cita con el mismo mecanismo y TTL que hoy (`CitationHandle`). El
  navegador sigue recibiendo solo handles opacos.

#### D3. Dos modos de respuesta

- **Texto legal**: el backend devuelve tal cual el resultado estructurado de
  `search_documents`. El agente no reescribe interpretación legal.
- **Datos estructurados (IDP): el agente razona, el backend redacta.** El
  agente encadena tools, decide qué documentos y campos responden a la
  pregunta, y cómo filtrarlos, agruparlos y ordenarlos. **No escribe texto
  libre.** Su salida final es un plan de respuesta estructurado (JSON):
  - referencias a campos devueltos por `get_extracted_fields` en ese mismo
    loop;
  - grupos con un criterio de una lista cerrada (por ejemplo "vence en el
    periodo", "con renovación automática");
  - orden;
  - agregados pedidos por tipo (recuento, mínimo, máximo), que **calcula el
    backend**, no el agente;
  - una introducción y transiciones elegidas de una lista cerrada de frases.

  El backend redacta cada línea con plantillas versionadas y pone el valor y
  la cita de cada campo. Una referencia desconocida, un criterio o una frase
  fuera de las listas o un JSON inválido activan el fallback. Así ninguna
  afirmación factual sobre los datos sale de texto generado por el modelo,
  tampoco las que no tienen cifras ("está vigente", "tres meses").
- **Preguntas mixtas** (datos y texto, por ejemplo "¿cuándo vence el contrato
  X y qué dice sobre la rescisión?"): la respuesta se compone de **bloques
  separados**. Un bloque de datos redactado por el backend según el plan del
  agente y un bloque de texto con el resultado de `search_documents` tal cual.
  El agente decide el orden y las transiciones, que salen de la misma lista
  cerrada. Cada bloque conserva sus propias citas. Si un bloque no valida, se
  omite y se indica; si no queda ninguno, se aplica el fallback.
- **Semántica de los campos IDP**: los campos aceptados y auto-aceptados se
  presentan como hechos. Los provisionales o pendientes de revisión se
  presentan marcados como "pendiente de revisión humana". Los ausentes o
  rechazados no se rellenan.

#### D3-bis. Cómo nombra el plan la fuente de un bloque de texto o de inventario (enmienda 2026-11-03, para revisar como cambio de arquitectura)

**Qué cambia.** En el plan, un bloque `text` o `inventory` ya no apunta a su fuente con el id de tool use
(`{"source": {"toolUseId": "<id>"}}`) sino con **la tool y la posición de la llamada** dentro del loop:
`{"source": {"tool": "search_documents", "call": 1}}` (o `list_matter_documents`), donde `call` cuenta desde 1 las
llamadas a esa tool en el orden en que el modelo las hizo, **contando también las que fallaron**. El servidor resuelve la
posición contra el registro de llamadas del mismo loop (`LoopEvidence.call_order`, en orden de stream) y aplica la misma
validación cerrada de antes. El prompt pasa a la versión 1.4.0.

**Por qué (evidencia).** Con el contrato anterior, 14 de 14 planes de la segunda ejecución real (2026-11-03) citaron un id que
no coincidía con ninguna llamada del stream (`sourceIdMatches: none`, ni parecido), mientras que los bloques de datos, que
apuntan a `documentId` y `field` visibles en el texto de los resultados, funcionaron siempre (`data-01/02/03/05`). El modelo no ve, o
no puede copiar con fiabilidad, el id que genera Harness; un contrato basado en copiarlo no puede funcionar, y el efecto era que
casi todas las respuestas de texto e inventario caían al resultado de la tool o al RAG.

**Qué NO cambia.** El texto se sigue imprimiendo **tal cual** desde el resultado de `search_documents` (el modelo no aporta
contenido, solo una posición); los límites de citas (`MAX_OUTPUT_CITATIONS`), los bloques de datos y todas las listas cerradas;
el fallback D5 (aprovechar el resultado válido y, si no, el RAG directo) y sus motivos; la regla de que ninguna llamada se imprime
dos veces. La posición solo puede señalar una llamada que **existe en el loop y devolvió un resultado válido**; si no, el bloque se
omite con un motivo cerrado (`unknown_source_call`, `source_call_unusable`). El modelo no puede señalar nada que el servidor no haya
recibido de una tool.

**Compatibilidad.** El parser sigue aceptando el id exacto (`{"toolUseId": ...}`), validado igual, pero el prompt ya no lo anuncia y no
se espera que el modelo lo use. El conjunto de datos congelado del eval de enrutado no se toca (sus planes simulados usan el id
exacto, que sigue siendo válido); el nuevo formato tiene sus propios tests.

#### D4. Loop y ejecución asíncrona

- **Guardrail de entrada antes del agente**: la pregunta pasa por el Guardrail
  de entrada (como hoy en `chat.py`) antes de crear el job. Si se bloquea, no
  se invoca a Harness ni se consume cuota del agente.
- Loop gestionado por Harness (Strands sobre Runtime), con `maxIterations` 3,
  `timeoutSeconds` 90 para el loop y un presupuesto de 25 s por tool. Como el
  chat es asíncrono, el presupuesto se dimensiona para el peor caso razonable
  (1–2 tools: unos 15–35 s) y no para el límite de 29 s de la API. El
  system prompt del agente se versiona y se fija por hash como los prompts
  existentes (`prompts/`), y el backend no acepta overrides de modelo, prompt,
  tools ni skills desde el cliente.
- **El chat agentic es asíncrono.** `POST /api/chat` autoriza, reserva cuota,
  crea un job ligado a sujeto, tenant, matter, conversación y correlación, y
  devuelve un `jobId`. Un worker (SQS → Lambda, el mismo patrón que IDP)
  invoca Harness. La UI consulta el resultado con un endpoint que vuelve a
  autorizar contra el job. Así se elimina la dependencia del límite de 29 s de
  la API pública.
- **Identidad del worker**: el worker carga la sesión que ya está guardada en
  servidor (`state.py`, `SessionRecord`) para construir el binding sellado. El
  token de acceso no viaja en el mensaje de la cola. Si el usuario cierra
  sesión, el job falla cerrado a propósito, porque ya no hay autorización para
  actuar en su nombre: se descarta sin respuesta, se libera la reserva de cuota
  y la UI pide repetir la pregunta. Para que un job no empiece con una sesión a
  punto de caducar, el backend solo lo crea si a la sesión le quedan al menos
  5 minutos; si no, pide volver a iniciar sesión. Alternativa descartada: una
  identidad de máquina (M2M, como IDP) que permita al job sobrevivir al cierre
  de sesión. Cubre un caso muy raro y obliga al agente a actuar sin una sesión
  viva del usuario.
- **Jobs**: se guardan con clave por tenant y matter y TTL de 1 hora; solo los
  pueden leer el sujeto y el matter que los crearon. Como el TTL de DynamoDB no
  es un borrado exacto, el borrado de matter del runbook de data governance
  (fase 14) incluye los jobs en su operación de scope.
- **Fiabilidad del worker**:
  - como máximo **1 reintento**, y solo ante errores transitorios
    (throttling, 5xx del proveedor, muerte de la Lambda). Los errores
    permanentes (validación, sesión cerrada, bug) marcan el job como fallido
    sin reintentar. Sin reintento, el usuario repetiría la pregunta con el
    mismo coste y peor experiencia;
  - **idempotencia**: el job guarda estado, número de intento y plazo del
    intento en curso, y cada transición es una escritura condicional. Un
    mensaje duplicado durante un intento vivo se ignora; tras un plazo vencido
    solo se retoma si quedan intentos. La cuota se reserva una vez por job y la
    memoria se escribe una sola vez al final;
  - **DLQ con alarma** para lo que falla dos veces. Un job que supera su
    plazo total se da por fallido al consultarlo y libera la reserva de cuota;
  - timeout de la Lambda worker 150 s (loop de 90 s + fallback + margen).
    Concurrencia máxima 2, configurable, aplicada con el `MaximumConcurrency`
    del event source mapping de SQS (como IDP) y **no** con concurrencia
    reservada de Lambda: así el exceso espera en la cola sin consumir
    recepciones ni acabar en la DLQ. Visibility timeout de la cola de 900 s
    (al menos 6 veces el timeout de la Lambda, recomendación de AWS) y
    `maxReceiveCount` 2, coherente con el reintento único. Métrica de espera
    en cola para ajustar la concurrencia con datos.
  - **Plazo total del job: 5 minutos.** Para que el reintento quepa en ese
    plazo, ante un error transitorio capturado el worker reduce la
    visibilidad del mensaje a 30 s (`ChangeMessageVisibility`) antes de
    devolverlo a la cola. Si la Lambda muere sin capturar el error (por
    ejemplo, por timeout), el mensaje no reaparece hasta pasados los 900 s:
    para entonces el job ya se ha dado por fallido y la reentrega se ignora
    por idempotencia. Es un caso raro y se acepta.

#### D5. Fallback determinista

Si el agente no llama a ninguna tool, falla, agota iteraciones o tiempo, o
produce una síntesis que no valida, el worker **primero aprovecha lo que ya
tiene**: si una tool devolvió un resultado válido (por ejemplo, la respuesta
grounded de `search_documents`), lo devuelve. Solo si no hay nada aprovechable
responde con el RAG directo actual. El motivo del fallback se registra como
metadato.

#### D6. Allowlist por acción, reutilizando el mecanismo existente

Se añade la `application_action` `agent_chat` a `_APPLICATION_TOOL_ALLOWLISTS`
(`backend/src/legaldesk/agent_integration.py`), solo con tools de lectura. El
backend escribe el registro de invocación en servidor; su id viaja en
`x-legaldesk-invocation-id`, una cabecera fija de la configuración MCP que el
modelo no controla; el interceptor rechaza cualquier tool que no esté en
`allowedTools` de ese registro (`gateway_interceptor.py`). El límite de
privilegios del agente no depende del prompt. Todas las tools del agente exigen
`matterId` (esquemas de los targets y lectura del cuerpo crudo en el
interceptor), así que el cliente añade al system prompt anclado una instrucción
construida en servidor con el selector de la sesión (`matter_selector_instruction`,
2026-11-03: sin ella el modelo no tenía el valor y las llamadas no llegaban al
target). El selector no es autoridad: el interceptor lo compara con el acceso del
usuario y con el registro de invocación, y cualquier otro valor se rechaza. Las acciones `metadata` y
`review` de la UI y el camino M2M de IDP no cambian.

#### D7. Memoria

El backend sigue siendo el único propietario de la memoria de conversación
(ADR-011). Harness se invoca sin memoria adjunta y recibe el contexto de
conversación que le pasa el backend: las últimas 6 parejas de pregunta y
respuesta final, con límite de tamaño. Nunca resultados intermedios de tools.

#### D8. Cuota

Se reutilizan los contadores mensuales existentes de `quota.py`: cada pregunta
agentic consume 1 de `chats` (que ya cuenta por pregunta) y 1 de `harness`. El
límite por defecto de `harness` sube de 100 a 300 para igualarlo con `chats`.
Las llamadas a Gateway que hace el agente dentro de un job **no** consumen el
contador `gateway` de los botones. La reserva se hace una vez por job al
crearlo y falla cerrada.

#### D9. Observabilidad

AgentCore Observability / CloudWatch. Primero, una prueba acotada que reactiva
ADOT con el opt-out de contenido y repite la prueba del marcador único de la
fase 11 (en esa fase el opt-out fue ineficaz y se desactivó ADOT,
`docs/phase-11-acceptance.md`).

- Si no filtra contenido, se mantiene así.
- Si filtra, ADOT queda activo como **riesgo residual aceptado**: datos
  sintéticos, retención de logs de 1–3 días, acceso IAM restringido y la regla
  de `AGENTS.md` §7 matizada por escrito.

En ambos casos, el worker emite atributos de metadatos propios: tools elegidas,
iteraciones, camino de salida, motivo del fallback, latencia, tokens, coste,
ids y hashes. Con datos reales: opt-out verificado o spans propios sin
contenido.

### Alternativas consideradas

- **Volver a meter los botones en el agente**: descartada (ADR-016 y el tercer
  smoke de la fase 13).
- **Texto libre del agente en las respuestas de datos**: descartado tras la
  revisión de PullBeacon (PB-001). Un filtro de cifras, fechas e importes no
  impide afirmaciones factuales sin números ("está vigente", "tres meses"), y
  comprobarlas con el Guardrail de grounding añadiría falsos bloqueos en
  textos cortos. La capacidad del agente para preguntas complejas está en
  planificar, filtrar y combinar fuentes, y eso se conserva. La redacción
  libre y su evaluación se dejan para un proyecto diseñado para ello.
- **Clasificador de intención + dispatch determinista**: más barato y fiable
  para una sola fuente, pero no encadena tools ni sintetiza. Se usa como
  referencia en la eval de routing.
- **El agente redacta también las respuestas de texto legal**: aplazada como
  alternativa condicionada. Abre el mayor riesgo de grounding, justo donde el
  Guardrail ya bloquea respuestas cortas. Se revisará si la eval muestra que
  las preguntas multifuente sobre texto son frecuentes.
- **Chat síncrono con presupuesto interno de tiempo**: descartado. La cadena
  completa puede superar los 29 s y el fallback se volvería el camino habitual.
- **`search_documents` dentro de la Lambda MCP**: descartado por el IAM y el
  timeout que añadiría.
- **Sin reintentos automáticos**: descartado. El usuario repetiría la pregunta
  con el mismo coste; con idempotencia, un reintento acotado es seguro.
- **Checkpoints de resultados de tools para reanudar jobs**: descartado. El
  loop de Harness no se puede reanudar, así que solo se ahorrarían las
  llamadas internas de una tool en un fallo poco frecuente, a cambio de una
  caché con contenido de documentos que habría que proteger y borrar. La
  ejecución durable (Step Functions, Temporal, workflows reanudables) es el
  patrón adecuado para agentes largos y queda fuera de este proyecto.
- **Ampliar la cuota de Gateway compartida**: descartado. El chat podría dejar
  sin cuota a los botones, y lo que cuesta dinero es la pregunta.
- **Loop propio en el backend (Converse con tool use y `toolChoice`)**: da más
  control entre iteraciones, pero deja Harness sin uso. La seguridad no depende
  del loop, porque cada tool call se reautoriza en el interceptor. Sigue siendo
  la salida prevista si Harness impide un control necesario (ADR-003).
- **Reorganizar los targets en `matter-knowledge` (MCP, lectura) y
  `review-queue` (Lambda, escritura)**: es el diseño de referencia, pero queda
  fuera de esta fase.

### Consecuencias

- Una pregunta RAG cuesta **4 llamadas LLM** (turno del agente que elige la
  tool, resolver, writer y el turno final que Harness ejecuta siempre tras
  recibir el resultado, aunque el backend descarte su texto) más el Guardrail,
  frente a las 2 de hoy.
- Infraestructura nueva: Lambda y target `search-documents`, cola y worker del
  chat, registros de job con TTL. La alarma de latencia de la API (p95 ≤ 3 s)
  no aplica al job; el job tendrá su propia métrica.
- La UI pasa a enviar y consultar el resultado, con estado "pensando…".
- Harness vuelve a estar en el camino crítico del chat; el fallback evita que
  sea un punto único de fallo.
- Aparece una métrica de producto nueva: el acierto del routing.
- **Estado tras T9** (decisión del propietario, 2026-11-03):
  - **El chat agentic queda encendido** para poder hacer demos, con una cuota
    desplegada de `harness` de **50 preguntas al mes**. El valor por defecto
    de D8 sigue siendo 300.
  - **Coste fijo:** las alarmas del job usan **métricas nativas** de SQS y
    Lambda, que son gratuitas, y solo se mantienen como personalizadas las que
    no tienen equivalente (como mínimo, la tasa de fallback). Objetivo: unos
    1 USD al mes.
  - **Alarmas** solo en la consola, sin SNS. Si hay que cortar el servicio,
    se pone la cuota de `harness` a 0: un único cambio de parámetro, sin crear
    ni borrar recursos.
  - Encender o apagar los flags supone actualizar dos pilas como operador
    (unos 10–15 minutos). No compensa para el coste residual.
  - **Bloque T9:** techo de 5 USD y 50 invocaciones de Harness. ADOT se apaga
    tras la sonda si aparece contenido en los traces.
- **CD** (decisión del propietario, 2026-11-02):
  - La infraestructura del worker (cola, DLQ, Lambda, event source mapping)
    vive en la pila `LegalDeskPhase14PublicEdge` y usa el mismo paquete que la
    app.
  - El CD code-only (D-029) se amplía para actualizar **también el código del
    worker** en el mismo change set. Así API y worker comparten el contrato
    del job y no se desfasan entre releases.
  - Implica cambiar `scripts/deploy_release.py` (cambios permitidos) y el rol
    IAM del CD (`infra/cloudformation/cd-iam.yaml`), que despliega un
    operador en T9.
  - Mientras tanto, cualquier cambio en la plantilla de Public Edge exige que
    el operador despliegue la plantilla antes de la siguiente promoción,
    porque el CD exige que sea idéntica a la desplegada.
  - `search-documents`, MCP e interceptor siguen desplegándose con change sets
    revisados. Ampliar el CD a todas las Lambdas es una mejora transversal
    registrada aparte.

### Riesgos y verificaciones previas

- **Límites de AWS verificados en la documentación (2026-11-01)**:
  - Gateway: "Timeout for a gateway invocation" 15 minutos por defecto,
    ajustable (página de quotas de AgentCore). Basta con subir el timeout de
    la Lambda `search-documents`, hoy en 10 s en los targets existentes. Hay
    un informe de usuario (agentcore-cli #1388) con cortes de unos 40 s, así
    que el plan fija un presupuesto de 25 s para la tool y lo mide en la
    primera prueba en vivo.
  - `InvokeHarness`: `messages` es una lista de `HarnessMessage` con rol
    `user | assistant`, así que el backend puede enviar el historial. La
    memoria de Harness se puede desactivar (`disabled`). **Comprobado en vivo el
    2026-11-03 (paso 11): Harness SÍ conserva el historial dentro de un mismo
    `runtimeSessionId` aunque la memoria esté desactivada.** Se aplica la
    alternativa prevista, afinada: `runtimeSessionId` nuevo en **cada
    invocación** (cada job y cada reintento de un job), para que un reintento
    tampoco herede estado oculto. El scope de memoria del binding no cambia: sigue
    alimentando la cabecera `x-legaldesk-memory-session-id` y el registro de
    invocación, que el interceptor compara entre sí y no con la sesión de
    runtime. Esto cumple la condición que ya fijaba este ADR; no lo modifica.
    La misma regla vale para toda llamada hecha para un binding con
    `HarnessInvoker.invoke` (las acciones de botón `metadata` y `review`): su
    `runtimeSessionId` es nuevo en cada llamada y el scope de memoria solo
    alimenta `actorId`, las cabeceras y el registro de invocación. Con la memoria
    de Harness desactivada (paso 4 de T9) nada depende de que ambos ids
    coincidan; adjuntar de nuevo la memoria de Harness (Fase 09,
    `EnablePhase09Memory=true`) exigiría volver a unirlos para esa ruta y es una
    decisión de ADR, no un parámetro de despliegue. Quien llama solo con
    `session_id` (smokes, sondas) elige su sesión a propósito y la conserva.
  - La memoria gestionada de Harness guarda eventos en bruto (mensajes y tool
    calls), incluidos resultados de tools con fragmentos de documentos. Eso
    choca con ADR-011 y con la política del backend (`extractionMode=SKIP`,
    expiración a 7 días, límite de tamaño, scope derivado en servidor y
    borrado desde data governance). Es el motivo de D7.
- **Limitación conocida, ajena a esta fase: sesión absoluta de 1 hora.** La
  sesión dura 1 hora desde el login (`session_ttl_seconds`, `http_app.py`), no
  se renueva con la actividad y no hay refresh token (el login PKCE solo
  obtiene el access token). Un usuario activo es expulsado a los 60 minutos.
  La solución (refresh token guardado en servidor, renovación silenciosa y
  revocación al cerrar sesión) es una mejora de identidad independiente y
  queda fuera de esta fase.
- **El Guardrail de grounding** sigue bloqueando respuestas en documentos
  sintéticos cortos (limitación conocida; esta fase no la corrige).
- **Contenido en traces** si el opt-out sigue siendo ineficaz: aceptado según
  D9.
- **Prompt injection en resultados de tools**: el agente solo tiene tools de
  lectura en el mismo matter y no escribe datos (D3), lo que limita el impacto.
