"""Explicit test-only server for offline browser acceptance; never deploy.

All provider SDK clients are patched by the shared integration fixture before
the real factory is built. Only loopback traffic is needed. Ctrl+C stops it.
"""
import json
import os
import threading
from unittest.mock import patch

from test_phase13_integration import Phase13ConcreteIntegrationTests
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
    # The fixture JWT expires after ten minutes.  Keep the manual offline
    # demo usable when the server was started before the guided login by
    # minting a fresh fictional token for each local code exchange.
    composition.token_exchange.token_supplier = lambda: fixture.fixture._token("alice")
    print(json.dumps({"baseUrl": base, "pid": os.getpid(), "providers": "LOCAL_DOUBLES_ONLY"}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
