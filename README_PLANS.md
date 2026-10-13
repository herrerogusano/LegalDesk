# LegalDesk — Planning Pack

Este documento conserva la planificación completa del proyecto **LegalDesk:
grounded legal assistant on Amazon Bedrock AgentCore**.

## Cómo orientarse

Los documentos de planificación viven en la raíz del repositorio y sirven como
referencia para entender el alcance, las decisiones y el orden de trabajo.

Orden recomendado:

1. `PROJECT_REQUIREMENTS.md`
2. `ARCHITECTURE_DECISIONS.md`
3. `AWS_COST_POLICY.md`
4. `AGENTS.md`
5. `CODEX_WORKFLOW.md`
6. `LEARNING_WORKFLOW.md`
7. `MASTER_PLAN.md`
8. Ejecutar las fases en orden:
   - `PLAN_00_ARCHITECTURE.md`
   - `PLAN_01_AGENTCORE_RUNTIME.md`
   - `PLAN_02_DOCUMENT_PIPELINE.md`
   - `PLAN_03_KNOWLEDGE_BASE.md`
   - `PLAN_04_GROUNDED_CHAT.md`
   - `PLAN_05_SYSTEM_PROMPT.md`
   - `PLAN_06_GUARDRAILS.md`
   - `PLAN_07_LAMBDA_TOOL.md`
   - `PLAN_08_GATEWAY_MCP.md`
   - `PLAN_09_MEMORY.md`
   - `PLAN_10_IDENTITY_ISOLATION.md`
   - `PLAN_11_DEPLOY_OBSERVABILITY.md`
   - `PLAN_12_EVALUATION_DEMO.md`

## Regla principal

No ejecutar varias fases por adelantado.

Cada fase debe:
- partir de la anterior en estado verde;
- cumplir sus criterios de aceptación;
- ejecutar los tests pertinentes;
- actualizar documentación si cambia una decisión;
- detenerse antes de crear o usar recursos AWS con coste no previamente aceptado.

## Objetivo técnico

Construir una aplicación pequeña pero con mentalidad de producción donde un usuario autenticado pueda:

- seleccionar un `matter`;
- subir documentos legales públicos o inventados;
- preguntar sobre ellos;
- recibir respuestas fundamentadas en fuentes recuperadas;
- ver citas;
- usar herramientas a través de AgentCore Gateway;
- crear una tarea de revisión humana;
- mantener contexto de conversación con memoria segura;
- demostrar aislamiento entre matters;
- inspeccionar trazas y métricas.

No es un producto jurídico real. No usar datos legales reales, confidenciales o
personales durante el desarrollo ni las demos.
