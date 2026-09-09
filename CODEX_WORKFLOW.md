# LegalDesk — Codex Workflow

## Objetivo

Trabajar con Codex de forma autónoma pero controlada y eficiente con los límites de la suscripción ChatGPT.

## Flujo por fase

### 1. Usuario indica fase
Ejemplo:

`Implementa PLAN_03_KNOWLEDGE_BASE.md siguiendo AGENTS.md.`

### 2. Supervisor prepara ejecución
Debe:
- leer el plan;
- leer solo contexto necesario;
- comprobar precondiciones;
- listar brevemente subtareas;
- identificar cualquier operación AWS potencialmente facturable.

### 3. Delegación
Por defecto:
- un worker Luna High.

El worker:
- implementa;
- prueba;
- corrige errores simples;
- entrega resumen.

### 4. Supervisor revisa
Revisión enfocada en:
- integración;
- seguridad;
- acceptance criteria;
- desviaciones de arquitectura;
- costes;
- calidad del resultado.

No repetir toda la implementación.

### 5. Cierre
No empezar la siguiente fase.

## Qué debe recibir un worker

Idealmente solo:
- objetivo;
- plan/subtarea;
- archivos relevantes;
- interfaces de entrada/salida;
- restricciones;
- tests/acceptance criteria.

Evitar:
- historial completo del proyecto;
- todos los planes;
- documentación irrelevante;
- logs largos no relacionados.

## Cuándo NO crear un subagente

No crear worker adicional para:
- cambiar una variable;
- corregir typo;
- actualizar una línea de documentación;
- ejecutar un comando;
- arreglar un fallo obvio tras un test.

## Cuándo sí puede valer la pena

Ejemplo:
- IaC y aplicación pueden implementarse de forma independiente sin tocar los mismos archivos;
- una investigación puntual bloquea al implementador;
- una revisión de seguridad requiere un segundo punto de vista por alto riesgo.

## Control de contexto

Antes de abrir archivos:
1. identificar cuáles hacen falta;
2. leer primero interfaces y tests;
3. expandir contexto solo si aparece una dependencia.

## Control de iteraciones

Si una prueba falla:
1. leer el error;
2. corregir la causa probable;
3. volver a ejecutar la prueba mínima;
4. ampliar tests después.

No repetir suites completas de forma ciega.

## Uso de Sol

Sol Medium:
- orquestación;
- integración;
- arquitectura;
- revisión final.

No usar Sol para:
- boilerplate;
- CRUD simple;
- tests rutinarios;
- cambios mecánicos;
- documentación derivada de código ya claro.

## Uso de Luna

Luna High:
- implementación;
- tests;
- refactors acotados;
- debugging normal;
- scripts;
- IaC rutinario.

## Branch workflow

Una branch por fase.

Al final:
- tests verdes;
- diff revisado;
- commit(s);
- usuario decide merge.

No comenzar la siguiente branch/fase hasta que la anterior esté aceptada.
