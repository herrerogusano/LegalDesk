# LegalDesk — AWS Cost & Safety Policy

## Objetivo

Evitar costes inesperados.

Este proyecto debe ser **cost-conscious by default**.

## Regla obligatoria

Antes de ejecutar una operación que:
- cree un recurso facturable;
- active una capacidad de pago;
- haga inferencia con coste;
- ingiera/indexe documentos con coste;
- genere almacenamiento/vectorización;
- habilite una característica con coste no trivial;

Codex debe:

1. identificar el recurso/API;
2. indicar por qué es necesario;
3. explicar de forma breve qué puede generar coste;
4. buscar una alternativa gratuita/local si existe;
5. detenerse y pedir permiso explícito al usuario si el coste no estaba ya aprobado para esa fase.

## Operaciones locales

Preferir:
- tests unitarios;
- mocks/fakes;
- fixtures locales;
- validación de schemas;
- linters;
- type checks;
- synth/diff de IaC sin deploy cuando sea suficiente.

## Bedrock / AgentCore

No asumir que "serverless" significa gratis.

Tratar como potencialmente facturables:
- llamadas de inferencia;
- Knowledge Base ingestion/retrieval;
- embeddings/vector store;
- AgentCore Runtime/Harness;
- Memory;
- Gateway;
- Observability/CloudWatch;
- evaluaciones;
- Lambda/API requests;
- DynamoDB;
- S3;
- transferencia de datos;
- cualquier recurso adicional creado por CDK/CLI.

## Cost Explorer

No usar APIs de coste facturables automáticamente.

Si se quiere consultar gasto:
- preferir consola/billing data existente;
- explicar si una API concreta tiene coste antes de llamarla.

## Presupuestos

Cuando sea razonable:
- documentar un presupuesto/alerta;
- mantener dataset pequeño;
- limitar número de documentos;
- limitar evaluaciones;
- evitar loops de pruebas contra modelos reales;
- usar tests locales para la mayoría de casos.

## Teardown

Toda fase que cree infraestructura debe:
- documentar cómo destruirla;
- evitar recursos huérfanos;
- etiquetar recursos del proyecto;
- comprobar que el teardown incluye buckets/índices/recursos que requieran pasos especiales.

## Nunca automáticamente

- habilitar recursos caros "por si acaso";
- escalar capacidad;
- aumentar retención de logs;
- crear recursos multi-region;
- ejecutar suites grandes de inferencia repetidamente;
- dejar polling continuo;
- crear recursos fuera del plan de fase.
