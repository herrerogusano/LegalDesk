# ADR-022 (propuesta) — Aislamiento multi-tenant en S3 con credenciales por tenant (ABAC)

**Estado: PROPUESTA, pendiente de aprobación del propietario.** No se incorpora a `ARCHITECTURE_DECISIONS.md` hasta aprobarla
(`AGENTS.md` §10). Primer paso de una evolución en tres partes: S3 (este ADR), DynamoDB y KB/Memory (futuros).

## Contexto

- Hoy cada Lambda tiene en su rol IAM el **tenant de la beta fijado** (`BetaTenantId`, `SearchTenantId`, `IDPReviewTenantId`;
  58 apariciones solo de `BetaTenantId`; inventario del 2026-11-02). El aislamiento entre clientes lo hace el **código** (autorización
  por petición, ya probada con tests cross-matter) y el IAM actúa como red de seguridad, pero **solo para un tenant**. Añadir un cliente
  obliga a tocar todas las plantillas.
- El inventario confirma que **todos los accesos a S3 ocurren después de conocer y autorizar el tenant**, con claves que empiezan por
  `tenants/<t>/…`, `quarantine/tenants/<t>/…` o `idp-artifacts/tenant=<t>/…`.
- DynamoDB es el paso difícil: muchas claves de estado son hashes sin tenant (`LEGALDESK#P14#STATE#<KIND>#<sha256>`), así que un
  `dynamodb:LeadingKeys` con etiqueta exigiría reestructurar claves y migrar datos. **Queda fuera de este ADR.**

## Decisión

1. **Un rol de datos por tenant, compartido y etiquetado (ABAC)**: `LegalDeskTenantDocumentAccess`. Su política solo permite S3 bajo
   el prefijo del tenant de la sesión:
   `arn:aws:s3:::<bucket>/tenants/${aws:PrincipalTag/tenant}/*`, `…/quarantine/tenants/${aws:PrincipalTag/tenant}/*` y
   `…/idp-artifacts/tenant=${aws:PrincipalTag/tenant}/*`, con las acciones que necesita cada flujo (Get/Head, Put, Copy, Delete,
   Tagging) y nada más.
2. **Credenciales temporales por petición**: tras la autorización, la Lambda llama a `sts:AssumeRole` sobre ese rol con
   `Tags=[{Key: tenant, Value: <tenant autorizado>}]` y usa esas credenciales **solo para S3**. Hay un único helper
   (`tenant_storage_session(context)`) que recibe el `RequestContext` ya autorizado; ningún otro código puede elegir la etiqueta.
3. **Confianza mínima**: el rol confía solo en los roles de ejecución de las Lambdas que leen o escriben documentos, con
   `sts:TagSession`, `aws:TagKeys = [tenant]` y `aws:RequestTag/tenant` que cumpla el patrón de ids de tenant.
4. **Los roles de ejecución pierden el acceso directo** a objetos de tenant en S3 (se quitan los `${BetaTenantId}` de S3). El acceso
   solo es posible a través del rol etiquetado.
5. **Caché**: credenciales por tenant cacheadas en el contenedor de la Lambda, con caducidad corta (propuesta: 15 minutos, con renovación
   anticipada), para no añadir una llamada a STS en cada petición.
6. **Configuración de recursos**: el prefijo del plan de GuardDuty y las reglas de EventBridge pasan de `quarantine/tenants/<beta>/` a
   `quarantine/tenants/`. El escáner de malware ya corrobora el tenant de la clave contra DynamoDB.

## Qué protege y qué no

- **Protege** de errores de construcción de claves y de referencias cruzadas: un fallo que construya mal la ruta, una clave
  manipulada o un resultado de la Knowledge Base de otro tenant. La lectura en S3 **falla por permisos**. La comprobación que ya hace el
  retrieval (HEAD del objeto canónico de cada resultado) pasa a ser una **barrera real**.
- **No protege** de un error en la resolución del tenant en el paso de autorización: si el código autoriza al tenant equivocado, la
  etiqueta será la equivocada. Por eso la etiqueta solo se calcula desde el `RequestContext` autorizado, en un único sitio y con tests.
- **No cubre** DynamoDB ni la Knowledge Base, que siguen protegidos por el código y el filtro de metadatos (pasos 2 y 3).

## Alternativas consideradas

- **Abrir el IAM a `tenants/*`**: descartada; se pierde la red de seguridad.
- **Una pila o un bucket por tenant (silo)**: descartada para este portfolio; multiplica el coste y la operación.
- **Un rol por tenant**: descartada; hay que crear uno por cliente y el resultado es el mismo que con un único rol etiquetado.
- **Etiqueta también por matter**: aplazada. Las claves de S3 incluyen el matter, así que es posible, pero la cuota y otros datos son
  por tenant. Se puede añadir después sin rehacer el diseño.

## Consecuencias

- Una llamada a STS por tenant cada 15 minutos por contenedor (latencia de decenas de ms en frío; STS no tiene coste).
- **Las URL prefirmadas de subida** se firman con las credenciales etiquetadas: dejan de valer cuando caduca la sesión, aunque la URL
  diga otra cosa. La sesión debe durar al menos lo mismo que la URL (hoy 900 s).
- **Textract** lee el documento con la identidad de quien lo llama: el worker de IDP debe llamarlo con las credenciales etiquetadas.
- Cambian las plantillas de todas las pilas con Lambdas que tocan documentos: app, worker, `search-documents`, MCP, review, IDP worker
  y OCR, malware scan y reconciliation. **Requiere un bloque en vivo** con change sets revisados (las lecciones de T9 aplican: rol de
  servicio code-only de Public Edge, recorte de nombres, lecturas de los `!GetAtt` vecinos).

## Puntos abiertos (comprobar antes de aprobar)

1. Documentación de AWS (revisada el 2026-11-03):
   - **Confirmado:** `${aws:PrincipalTag/…}` se puede usar en la parte de recurso del ARN, incluidos los prefijos de objetos S3
     (guía de IAM, "Policy variables": ejemplo `arn:aws:s3:::bucket/${aws:PrincipalTag/dept}/*`). Si la etiqueta falta, el recurso no
     casa con nada, así que falla cerrado.
   - **Confirmado:** una sesión obtenida por encadenamiento de roles dura como máximo 1 hora, y una URL prefirmada caduca cuando
     caducan sus credenciales (guía de S3, "presigned URLs").
   - **Pendiente:** la sintaxis exacta de `sts:TagSession` con `aws:RequestTag/tenant` y `aws:TagKeys` en la confianza (patrón conocido;
     se verificará con una simulación en T1).
   - **Pendiente, sin fuente oficial encontrada:** si Textract lee el objeto de S3 con las credenciales del llamante o con las del
     servicio. T1 lo verificará con una prueba acotada antes de mover el worker de IDP.
2. **Reconciliation** recorre varios tenants por diseño (TTL y limpiezas). Decidir si asume el rol una vez por tenant o si se queda con
   un permiso de borrado acotado a `quarantine/`.
3. **Malware scan:** el tenant sale de la clave del objeto antes de corroborarlo. Decidir si asume el rol con ese tenant o si mantiene
   un permiso propio limitado a la copia de `quarantine/` a `tenants/`.
4. **Bucket de artefactos de IDP**: hoy es el mismo bucket de origen; confirmar que el patrón `idp-artifacts/tenant=` cubre todas las
   rutas de artefactos.
5. **Pruebas:** tests de que ningún rol de ejecución puede leer `tenants/*` directamente, y simulación de IAM con etiquetas de dos
   tenants distintos (A lee A; A no lee B).
