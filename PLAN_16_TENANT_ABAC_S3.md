# PLAN 16 — Aislamiento multi-tenant en S3 con ABAC (borrador)

**Borrador, pendiente de la aprobación del ADR-022.** No se ejecuta antes de cerrar la fase 15 (T9 completo).

## Objetivo

Que el acceso a los documentos en S3 esté limitado por AWS al tenant autorizado de cada petición, mediante credenciales temporales
etiquetadas, y que añadir un tenant no exija tocar plantillas.

## Conceptos a demostrar

- ABAC con etiquetas de sesión (`sts:AssumeRole` + `TagSession` + `${aws:PrincipalTag/…}`);
- defensa en profundidad: la autorización en código, más el IAM que hace cumplir el tenant;
- qué protege y qué no protege el ABAC (resolución del tenant frente a construcción de claves);
- migrar permisos fijados a dinámicos sin cortes.

## Trabajo

- **T1. Verificaciones (sin AWS):** los puntos abiertos 1 a 4 del ADR-022 contra la documentación actual. Las respuestas cierran el
  ADR.
- **T2. Rol de datos y helper (local):** plantilla del rol `LegalDeskTenantDocumentAccess` (política por prefijo con
  `${aws:PrincipalTag/tenant}`, confianza con `TagSession` y condiciones) y `tenant_storage_session(context)` con caché, renovación y
  validación de la etiqueta. Tests: que la etiqueta solo sale del contexto autorizado, la caducidad y el formato del tenant.
- **T3. Migración del código (local):** cada acceso a S3 de las 9 Lambdas pasa por el helper: app (prefirmadas, HEAD, sidecars),
  worker y `search-documents` (HEAD del retrieval), MCP y review (artefactos y origen), IDP worker y OCR (incluido Textract), malware
  scan y reconciliation, según las decisiones de los puntos 2 y 3. Tests cross-tenant con dobles: con la etiqueta A, una clave de B
  falla.
- **T4. IaC (local):** quitar los `${BetaTenantId}` de S3 de los roles de ejecución; añadir `sts:AssumeRole` sobre el rol de datos;
  generalizar el prefijo de GuardDuty y EventBridge. `cfn-lint`, perfiles de change set (con las lecciones de T9: rol de servicio de
  la pila, recorte de nombres, lecturas de `!GetAtt`, contexto en la simulación) y tests de que ningún rol de ejecución lee
  `tenants/*` directamente.
- **T5. Runbook y coste:** bloque en vivo sin cortes. Primero se crea el rol de datos; después se despliega el código, que usa el rol
  pero sigue teniendo el permiso antiguo; por último se quita el permiso antiguo. Coste: STS gratis y sin infraestructura siempre
  encendida.
- **T6. Bloque en vivo (aprobación de coste):** despliegue en el orden de T5; simulación de IAM con dos tenants; smoke de subida,
  indexación, chat RAG, IDP y agente; prueba negativa (petición manipulada a la clave de otro tenant → `AccessDenied`).
- **T7. Explicación para entrevistas:** `docs/phase-16-tenant-isolation-explained.md` con el problema, el diagrama, la analogía, las
  decisiones, los límites y las preguntas típicas con su respuesta.

## Criterios de aceptación

1. Ningún rol de ejecución puede leer ni escribir objetos de tenant en S3 sin el rol etiquetado (test y simulación).
2. Con credenciales del tenant A, cualquier acceso a claves de B da `AccessDenied` (test con dobles y simulación en vivo).
3. Todos los flujos funcionan en la beta: subida, malware, indexación, chat RAG, IDP, agente y borrado de datos.
4. Añadir un tenant no exige tocar plantillas.
5. Se documenta qué no protege el ABAC (DynamoDB, KB, resolución del tenant).

## Estimación

20–30 horas humanas; la mayor parte en T3 (9 Lambdas) y en T6 (bloque en vivo).
