# PLAN 09 — AgentCore Memory

## Objetivo

Añadir continuidad conversacional segura y decidir explícitamente qué puede persistir entre sesiones.

## Conceptos a demostrar

- statelessness;
- actorId;
- sessionId;
- short-term events;
- long-term memory;
- strategies/namespaces;
- data minimization.

## Precondiciones

- agent/tools integrados;
- user identity estable;
- conversation IDs definidos.

## Trabajo

1. Crear AgentCore Memory.
2. Definir `actorId` desde identidad verificada.
3. Definir `sessionId` por conversación.
4. Persistir short-term conversation context.
5. Verificar reload/continuity con misma sesión.
6. Verificar aislamiento entre actores/sesiones.
7. Para long-term:
   - mantener deshabilitado inicialmente o
   - habilitar solo allowlist de preferencias inocuas.
8. Prohibir explícitamente:
   - raw legal text;
   - legal conclusions;
   - sensitive case facts;
   - secrets.
9. Definir namespace/scoping.
10. Añadir delete/cleanup strategy conceptual.
11. Documentar qué es memory y qué sigue perteneciendo a KB/DB.

## Criterios de aceptación

- misma sesión mantiene contexto;
- otra sesión/actor no recibe contexto indebido;
- memory keys/scopes se derivan server-side;
- long-term policy está implementada o explícitamente rechazada con razones;
- no se guarda raw document text como long-term memory.

## Tests / verificación

- same-session continuity;
- different-session isolation;
- different-actor isolation;
- prohibited-memory case;
- safe-preference case si long-term se habilita.

## Coste y seguridad

Memory puede generar coste y retención.
Usar expiración razonable.
No crear estrategias long-term innecesarias.
No almacenar datos reales.

## Outputs esperados

- Memory IaC/config;
- memory adapter;
- scoping tests;
- policy docs.

## Definition of Done

- Código y documentación coherentes.
- Tests relevantes verdes.
- Acceptance criteria comprobados uno a uno.
- Sin secretos ni datos legales reales.
- Recursos AWS y posibles costes listados.
- No se ejecuta automáticamente la siguiente fase.
