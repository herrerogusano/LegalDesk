# PLAN 15 — Chat agentic de solo lectura (AgentCore Harness)

Decisión de referencia: **ADR-021** en `ARCHITECTURE_DECISIONS.md`. Este plan no
reabre decisiones del ADR; si una tarea exige desviarse, se detiene y se
consulta (`AGENTS.md` §10).

## Objetivo

Que la caja de chat la gestione un agente de Harness que decide qué tools de
lectura usar (texto grounded, campos IDP, inventario), con respuestas
validadas por el backend, ejecución asíncrona, fallback determinista y
observabilidad del loop.

## Conceptos a demostrar

- agente que decide (routing y encadenado de tools) frente a acciones
  deterministas (ADR-016);
- límite de privilegios del agente fijado en servidor, no en el prompt;
- el agente sintetiza datos verificables pero no redacta interpretación legal;
- ejecución asíncrona de agentes (job + worker + sondeo);
- observabilidad de agentes: traces del loop, acierto del routing, fallback,
  latencia, tokens y coste;
- eval de routing como gate de CI.

## Precondiciones

- ADR-021 aprobado (2026-11-01).
- Fase 14 e IDP aceptadas; `developer` desplegada y CI en verde.
- Harness `LegalDeskPhase01` desplegado (hoy sin uso en producción).
- Antes de cualquier operación AWS facturable: aprobación explícita del
  propietario según `AWS_COST_POLICY.md`.

## Trabajo

Orden pensado para validar primero en local y llegar a AWS con un único bloque
de pruebas en vivo.

### T1. Contratos y prompt del agente (local)

1. Schemas de `search_documents` y `get_extracted_fields` (en lote, máximo 20
   documentos, solo campos del registro IDP).
2. Contrato de salida final del agente: plan de respuesta JSON con bloques
   (`data`, `text`). Para `data`: referencias a campos, grupos con criterios
   de una lista cerrada, orden y agregados pedidos por tipo. Además,
   introducción y transiciones de una lista cerrada y `suggestReview`
   opcional. Sin texto libre.
3. Plantillas versionadas del backend para redactar líneas de datos,
   agregados, introducciones y transiciones (en `prompts/` o junto al
   validador, fijadas por hash).
4. System prompt del agente versionado en `prompts/` y fijado por hash; tests
   de que el cliente no puede sobrescribir modelo, prompt, tools ni skills.

### T2. Tools nuevas (local)

1. `search_documents`: Lambda propia que reutiliza el pipeline grounded actual
   (Retrieve → resolver → writer → Guardrail → citas) a partir del grant del
   interceptor. Presupuesto de 25 s. Devuelve sus citas en el resultado; no
   escribe memoria ni estado de conversación.
2. `get_extracted_fields` en `mcp_server.py`, reutilizando la proyección IDP de
   `get_document_metadata`, con la semántica de ADR-021 D3 (aceptados =
   hechos; provisionales marcados; ausentes y rechazados vacíos).
3. Interceptor: target `search-documents` y nuevas combinaciones
   target/tool en la lista exacta; `application_action` `agent_chat` con solo
   tools de lectura en `_APPLICATION_TOOL_ALLOWLISTS`.

### T3. Validador de respuesta y fallback (local)

1. Validador determinista del plan: referencias existentes y devueltas en
   ese loop; criterios, tipos de agregado, introducciones y transiciones
   dentro de sus listas cerradas; bloques de texto idénticos al resultado de
   `search_documents`; citas por bloque.
2. Renderizado del bloque de datos con las plantillas: valor y cita de cada
   campo, agregados calculados por el backend y semántica de campos
   provisionales de ADR-021 D3.
3. Árbol de salida: grounded tal cual / síntesis de datos / mixto por bloques /
   aprovechar un resultado válido de tool si el loop se corta / fallback a RAG
   directo, con el motivo registrado.
4. Creación de handles de cita por el worker a partir de las citas devueltas
   por `search_documents`, con el mecanismo y TTL actuales.

### T4. Chat asíncrono (local)

1. `POST /api/chat`: autoriza, aplica el Guardrail de entrada, comprueba que a
   la sesión le quedan al menos 5 minutos, reserva cuota una vez por job, crea
   el job (clave por tenant y matter; sujeto, conversación, correlación; TTL de
   1 hora) y devuelve `jobId`.
2. Worker SQS → Lambda (timeout 150 s; concurrencia máxima 2 configurable
   mediante `MaximumConcurrency` del event source mapping, no concurrencia
   reservada; visibility timeout de la cola de 900 s; `maxReceiveCount` 2):
   carga la sesión en servidor, construye el binding sellado `agent_chat`,
   invoca Harness (sin memoria; últimas 6 parejas de historial enviadas por el
   backend; `maxIterations` 3; `timeoutSeconds` 90), valida y guarda el
   resultado. Sesión cerrada → falla cerrado y libera la reserva.
3. Fiabilidad: como máximo 1 reintento y solo ante errores transitorios;
   estado, intento y plazo del job con escrituras condicionales (idempotencia
   frente a reintentos y entregas duplicadas de SQS); DLQ con alarma; un job
   que supera su plazo total se da por fallido al consultarlo y libera la
   reserva.
4. `GET` del job: vuelve a autorizar contra sujeto y matter del job.
5. Memoria: el backend sigue siendo el único propietario (pregunta y respuesta
   final, escritas una sola vez; nunca resultados intermedios de tools).
6. Data governance: el borrado de matter incluye los jobs en su operación de
   scope; actualizar `docs/phase-14-data-governance-runbook.md`.
7. Atajo síncrono de campo seleccionado sin cambios.

### T5. Cuota

Reutilizar los contadores de `quota.py`: cada pregunta agentic consume 1 de
`chats` y 1 de `harness` (límite por defecto de `harness` de 100 a 300). Las
llamadas a Gateway dentro de un job no consumen el contador `gateway` de los
botones.

### T6. Frontend

Envío y sondeo, estado "pensando…", render de bloques `data` y `text` con sus
citas, botón de `suggestReview` que usa el flujo de revisión existente y
mensaje de "vuelve a enviar la pregunta" cuando un job falla por sesión.

### T7. Observabilidad

1. Atributos de metadatos emitidos por el worker: tools elegidas, iteraciones,
   camino de salida, motivo del fallback, latencia por tramo, tokens y coste
   estimado, ids y hashes; sin contenido.
2. Métricas y alarmas propias del job (no la de p95 ≤ 3 s de la API):
   duración, fallos, fallbacks, mensajes en la DLQ y tiempo de espera en cola.
3. Configuración preparada para reactivar ADOT en Harness con el opt-out
   (se ejecuta en T9).

### T8. Eval de routing (local y CI)

1. Dataset sintético de 25–30 preguntas con la tool o tools esperadas y el
   camino de salida esperado: texto, dato, inventario, mixtas, fuera de
   ámbito, intentos de inyección y de cross-matter.
2. Modo offline determinista en CI con un agente simulado: valida contratos,
   validador, fallback y allowlist.
3. Modo real (Bedrock + Harness) solo en T9, con presupuesto de llamadas fijo.

### T9. Bloque de pruebas en vivo (requiere aprobación de coste)

Un único bloque acotado, con change sets revisados y sin reintentos
automáticos:

1. Despliegue de la Lambda `search-documents`, el target, la cola y el worker,
   y la actualización de interceptor, MCP y Harness (sin memoria adjunta).
2. **Comprobación de sesión de Harness**: confirmar que, sin memoria, el mismo
   `runtimeSessionId` no guarda historial propio. Si lo guarda, usar un
   `runtimeSessionId` nuevo por job.
3. **Prueba de ADOT**: reactivar ADOT con el opt-out de contenido y repetir la
   prueba del marcador único de la fase 11. Si el marcador aparece, aplicar el
   riesgo residual aceptado (ADR-021 D9): retención de 1–3 días, IAM
   restringido y `AGENTS.md` §7 matizado por escrito.
4. Eval de routing en modo real.
5. Comprobar que el stream de eventos de Harness conserva los resultados de
   tools ya completados aunque el loop se corte después (base del
   aprovechamiento de D5). Si no los conserva, el fallback va directamente a
   RAG.
6. Smoke del navegador: pregunta de texto, de dato, mixta, de inventario,
   fallback, cross-matter denegado, apertura de una cita, propuesta de
   revisión y cierre de sesión con un job en curso.

## Criterios de aceptación

1. Una pregunta libre del chat la resuelve el agente eligiendo tools; se
   comprueba en traces y en metadatos.
2. El agente no puede invocar tools de review ni de otro matter: las rechaza
   el interceptor por el registro de invocación, con test y comprobación en
   vivo.
3. Las respuestas de texto legal son idénticas al resultado de
   `search_documents`; en las de datos todo el texto sale de plantillas del
   backend y ninguna afirmación factual procede de texto del modelo; las
   mixtas se presentan por bloques con sus citas.
4. Fallback a RAG directo en los casos de ADR-021 D5, con motivo registrado.
5. Chat asíncrono sin errores por el límite de 29 s; sesión cerrada → job
   cerrado y reserva de cuota liberada; un mensaje duplicado o reintentado no
   duplica respuesta, memoria ni cuota.
6. Cada pregunta agentic consume `chats` y `harness`, y no consume el contador
   `gateway` de los botones.
7. La memoria solo contiene pregunta y respuesta final; las citas de las
   respuestas del agente se pueden abrir desde la UI; la pregunta bloqueada por
   el Guardrail de entrada no invoca a Harness.
8. Eval de routing: el acierto de la tool en modo real alcanza el umbral
   fijado en T8 antes de ejecutarla (propuesta: ≥ 90 %), con 0 violaciones de
   allowlist y 0 planes fuera de las listas cerradas aceptados por el
   validador.
9. Observabilidad: trace o metadatos por pregunta con tools, iteraciones,
   camino de salida, latencia, tokens y coste; resultado de la prueba de ADOT
   documentado.
10. Botones, cola de revisión, pipeline IDP y atajo de campo seleccionado sin
    regresiones (suite existente en verde).

## Tests / verificación

- unit: contratos, validador del plan (referencias desconocidas, criterios y
  frases fuera de lista, JSON inválido, bloques mixtos), renderizado con
  plantillas y agregados, árbol de salida y aprovechamiento de
  resultados, cuota, TTL del job, sesión con menos de 5 minutos, Guardrail de
  entrada, handles de cita;
- fiabilidad: entrega duplicada, reintento tras error transitorio, error
  permanente sin reintento, plazo vencido, DLQ;
- autorización: `agent_chat` frente a tools de review, cross-matter, invocación
  falsificada o caducada, job leído por otro sujeto;
- integración local: worker con Harness simulado (eventos de tool use);
- CI: eval de routing offline;
- suites existentes, lint e IaC lint de las plantillas modificadas;
- en vivo: bloque T9.

## Coste y seguridad

- Despliegue: el CD actual solo actualiza la Lambda de la aplicación (D-029).
  La Lambda `search-documents`, el worker, la cola y los cambios en MCP,
  interceptor y Harness se despliegan con change sets revisados por un
  operador, como el resto de Lambdas de fuera de la app. **Mejora registrada
  aparte**: ampliar el CD code-only a todas las Lambdas.
- Facturable: invocaciones de Harness/Runtime, Bedrock (4 llamadas por
  pregunta de texto), Lambdas nuevas, SQS, DynamoDB, CloudWatch (ADOT,
  métricas y logs). Estimación del bloque T9: del orden de 1–3 USD; se
  confirmará con el desglose antes de pedir aprobación.
- No se añade infraestructura siempre encendida.
- Datos sintéticos únicamente; nada de contenido en logs propios.
- Cambios de IAM e infraestructura solo con change sets revisados y fuera de
  las credenciales de CD code-only.

## Estimación

28–40 horas humanas: T1–T3 unas 9–11 h, T4–T5 unas 8–11 h (incluida la
fiabilidad del worker), T6 unas 4–6 h, T7–T8 unas 4–6 h y T9 con su
documentación unas 3–6 h.

## Outputs esperados

- Lambda y target `search-documents`; tool `get_extracted_fields`;
- worker y endpoints del chat asíncrono;
- validador y árbol de salida;
- prompt del agente versionado;
- cuota `agent_chat`;
- frontend de envío y sondeo con bloques;
- dataset y runner de la eval de routing;
- `docs/phase-15-acceptance.md` con evidencia, costes y limitaciones.

## Definition of Done

- Código y documentación coherentes con ADR-021.
- Tests relevantes y eval offline en verde.
- Criterios de aceptación comprobados uno a uno.
- Sin secretos, sin datos legales reales y sin contenido en telemetría propia.
- Recursos AWS y costes reales listados.
- No se ejecuta automáticamente la siguiente fase.
