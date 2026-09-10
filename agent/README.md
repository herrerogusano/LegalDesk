# Agent

Phase 01 uses the managed AgentCore Harness. The agent loop is configuration,
not custom runtime code; the Python package in this directory is the narrow
application-side invocation adapter and local test double.

## Local tests

```bash
python -m unittest discover -s tests -v
```

## Optional AWS client dependency

```bash
python -m pip install -e agent
```

Agent code may consume only a server-built authorized request context. It must
never authorize access itself or forward model/configuration overrides from an
untrusted caller.
