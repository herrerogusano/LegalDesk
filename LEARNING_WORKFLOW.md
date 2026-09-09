# LegalDesk — Learning Workflow

Este archivo es para el trabajo conjunto usuario + ChatGPT antes y después de cada fase.

Los planes ya existen desde el inicio, pero **antes de pedir a Codex que implemente una fase**, el usuario puede avisar a ChatGPT para hacer el estudio previo.

## Estudio previo recomendado

Duración conceptual corta, sin intentar memorizar documentación.

### Paso 1 — Problema
Responder:
- ¿qué problema intenta resolver esta fase?
- ¿qué fallaría sin este componente?

### Paso 2 — Modelo mental
Explicar con un diagrama pequeño.

Ejemplo Knowledge Base:

`S3 → ingestion → index → retrieval filtrado → passages → LLM → answer + citations`

### Paso 3 — Comparación
Contrastar el concepto con el más parecido.

Ejemplos:
- Harness vs Runtime
- S3 vs Knowledge Base
- retrieval vs model access
- Guardrails vs authorization
- system prompt vs policy vs tool description
- function vs Gateway target vs MCP tool
- short-term vs long-term memory
- trace vs log vs metric

### Paso 4 — Predicción del usuario
2–4 preguntas antes de implementar.

No hace falta conocer la respuesta perfecta. El objetivo es crear una hipótesis.

### Paso 5 — Implementación con Codex
Ejecutar solo el `PLAN_XX`.

### Paso 6 — Post-mortem
Después de Codex:
- revisar 3–5 archivos/recursos importantes;
- seguir una request real de extremo a extremo;
- comprobar qué predicciones eran correctas.

### Paso 7 — Mini entrevista
3–5 preguntas:
- explicar el componente;
- justificar por qué se usa;
- explicar qué riesgo controla;
- explicar qué alternativa existe.

## Conceptos por fase

### Phase 00
- threat model
- tenant vs matter
- trust boundaries
- source of truth

### Phase 01
- agente
- orchestration loop
- AgentCore Harness
- AgentCore Runtime
- session isolation

### Phase 02
- S3 vs metadata DB
- upload pipeline
- data ownership
- ingestion states

### Phase 03
- RAG
- embeddings
- chunks
- retrieval
- metadata filtering
- citations

### Phase 04
- grounded generation
- not-found behavior
- source propagation
- retrieval quality vs generation quality

### Phase 05
- system prompt
- tool descriptions
- code policy
- authorization policy

### Phase 06
- Bedrock Guardrails
- prompt attacks
- PII
- contextual grounding
- why guardrails are not auth

### Phase 07
- agent tool
- Lambda
- side effects
- human escalation

### Phase 08
- MCP
- AgentCore Gateway
- remote tool
- tool boundary
- identity propagation

### Phase 09
- stateless vs stateful
- actor
- session
- short-term memory
- long-term memory
- namespace

### Phase 10
- Cognito/OIDC
- claims
- authentication vs authorization
- tenant isolation
- defense in depth

### Phase 11
- trace
- span
- metric
- log
- correlation ID
- IaC and teardown

### Phase 12
- evaluation datasets
- groundedness
- security tests
- regression tests
- production gaps

## Resultado esperado al terminar

El usuario debería poder dibujar y explicar:

`Browser`
`→ Identity`
`→ AgentCore Harness/Runtime`
`→ Retrieval / Knowledge Base`
`→ Guardrails`
`→ Gateway`
`→ MCP + Lambda`
`→ Memory`
`→ Observability`

Y contestar:
- qué hace cada caja;
- qué datos recibe;
- en qué confía;
- qué permiso necesita;
- qué riesgo controla;
- qué pasaría si la quitamos.
