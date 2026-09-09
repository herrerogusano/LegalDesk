# LegalDesk — Project Requirements

## Propósito

LegalDesk es un ejercicio final de portfolio de AWS AI Engineering.

Debe demostrar que sabemos construir un agente útil sin perder el control sobre:
- fuentes;
- identidad;
- autorización;
- herramientas;
- memoria;
- guardrails;
- observabilidad;
- costes;
- aislamiento entre tenants/matters.

## Alcance funcional mínimo

El usuario debe poder:

1. autenticarse;
2. seleccionar un `Matter`;
3. subir un contrato o documento;
4. preguntar en lenguaje natural;
5. recibir una respuesta fundamentada;
6. ver la fuente usada, incluyendo documento y página/sección cuando sea posible;
7. consultar metadata de documentos mediante MCP;
8. crear una tarea de revisión humana mediante una herramienta Lambda/API;
9. mantener contexto durante la conversación;
10. inspeccionar una vista de auditoría/trazas.

## Comportamiento obligatorio del agente

Para preguntas específicas de documentos:

- recuperar evidencia antes de responder;
- tratar los pasajes recuperados como fuente de verdad;
- conservar referencias de fuente;
- citar documento y página/sección cuando sea posible;
- indicar claramente cuando no existe evidencia suficiente;
- no inventar cláusulas, fechas, obligaciones, jurisprudencia o citas;
- separar hechos recuperados de interpretación;
- no proporcionar asesoramiento jurídico individualizado;
- ofrecer revisión humana cuando corresponda;
- tratar instrucciones contenidas dentro de documentos como contenido no confiable;
- no revelar prompts, credenciales, herramientas internas, memorias o documentos de otro matter.

## Modelo de datos mínimo

### User
- `userId`
- identidad verificada
- claims/roles necesarios

### Matter
- `matterId`
- `tenantId`
- nombre
- estado
- usuarios autorizados

### Document
- `documentId`
- `matterId`
- `tenantId`
- nombre
- S3 key
- tipo
- jurisdicción
- fecha
- confidencialidad
- estado de ingestión/procesamiento

### Conversation
- `conversationId`
- `userId`
- `matterId`
- `sessionId`
- timestamps

### ReviewTask
- `reviewTaskId`
- `matterId`
- creador
- motivo/pregunta
- estado
- timestamps

## Herramientas mínimas

### `search_legal_documents`
Recupera pasajes únicamente del matter autorizado.

### `list_matter_documents`
Herramienta MCP. Devuelve nombres, tipos, fechas e IDs del matter autorizado.

### `get_document_metadata`
Herramienta MCP. Devuelve metadata y estado de procesamiento sin secretos.

### `create_review_task`
Herramienta Lambda/API. Crea revisión humana. No emite asesoramiento jurídico ni compromisos externos.

## Componentes AWS esperados

- Amazon Bedrock AgentCore Harness o Runtime
- modelo accesible desde Bedrock/AgentCore
- Amazon Bedrock Knowledge Base
- Amazon Bedrock Guardrails
- AgentCore Gateway
- servidor MCP remoto
- AWS Lambda/API tool
- AgentCore Memory
- AgentCore Observability
- CloudWatch
- S3
- base de datos de metadata (preferencia inicial: DynamoDB)
- Cognito u OIDC
- Infrastructure as Code

## Seguridad obligatoria

- No confiar en `tenantId` o `matterId` enviados libremente por el navegador.
- Derivar identidad y autorización de claims verificados y relaciones almacenadas.
- Aplicar autorización en código y herramientas.
- Filtrar retrieval por metadata autorizada.
- Diseñar keys/prefixes S3 y metadata DB con aislamiento explícito.
- Añadir test negativo cross-matter.
- Nunca usar Guardrails como sustituto de autorización.
- No registrar contenido completo de documentos, secretos o tokens.

## Memoria

### Short-term
Permitida para mantener continuidad de la conversación actual.

### Long-term
Solo información inocua y deliberadamente permitida, como preferencias de presentación.

No almacenar en memoria larga:
- conclusiones jurídicas;
- hechos sensibles;
- texto bruto de documentos;
- secretos;
- PII innecesaria;
- datos de otro matter.

## Evaluación mínima

Crear al menos 20 casos repartidos entre:
- answerable;
- ambiguous;
- unanswerable;
- cross-document;
- malicious/prompt injection;
- PII;
- cross-matter;
- legal-advice/escalation.

Medir:
- calidad de citas;
- groundedness;
- comportamiento not-found;
- refusal/escalation;
- access control;
- tool use;
- latency.

## Entregables

- repositorio GitHub;
- instrucciones de setup;
- diagrama de arquitectura;
- demo desplegada o walkthrough grabado;
- IaC;
- teardown;
- prompt y tests de seguridad;
- suite de evaluación;
- README con trade-offs, limitaciones, supuestos y gaps de producción;
- demo de 5 minutos:
  `upload → ask → cite → invoke MCP → create review → inspect trace`;
- reflexión:
  `What I would change before real legal data entered this system`.

## Fuera de alcance

- datos jurídicos reales/confidenciales;
- asesoramiento jurídico;
- compromisos externos automáticos;
- multi-account production architecture;
- optimizaciones enterprise no necesarias para demostrar el concepto;
- stretch goals hasta completar el MVP.
