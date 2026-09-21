"""Explicit test-only server for offline browser acceptance; never deploy.

All provider SDK clients are patched by the shared integration fixture before
the real factory is built. Only loopback traffic is needed. Ctrl+C stops it.
"""
import json
import os
import threading
from unittest.mock import patch

from test_phase13_integration import Phase13ConcreteIntegrationTests
from phase13_gateway_fixture import invoke_local_gateway
from legaldesk.observability import InMemoryTelemetrySink


def main():
    fixture = Phase13ConcreteIntegrationTests()
    with patch("legaldesk.application.DEFAULT_TELEMETRY_SINK", InMemoryTelemetrySink()):
        fixture.setUp()
    composition = fixture.app.composition
    base = f"http://localhost:{fixture.port}"
    composition.allowed_origins = frozenset({base})
    composition.public_base_url = base
    composition.redirect_uri = f"{base}/callback"
    fixture.fixture.put_server.server.allowed_origin = base
    fixture.fixture.agentcore.invoke_harness = lambda **kwargs: invoke_local_gateway(composition, fixture.fixture.agentcore.invoke_calls, **kwargs)
    print(json.dumps({"baseUrl": base, "pid": os.getpid(), "providers": "LOCAL_DOUBLES_ONLY"}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
