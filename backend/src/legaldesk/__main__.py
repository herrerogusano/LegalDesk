"""Explicit loopback entry point for the LegalDesk application.

The runnable application has no local fake/provider fallback: ``--allow-aws``
is required before the production composition can construct SDK clients.
"""

from __future__ import annotations

import argparse
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .application import build_aws_composition
from .http_app import create_http_app


class QuietRequestHandler(WSGIRequestHandler):
    """Do not write request URLs, callback codes, or query strings to stdout."""

    def log_message(self, *_args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LegalDesk loopback API")
    parser.add_argument("--allow-aws", action="store_true", help="explicitly permit AWS SDK/resource construction")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.host not in {"localhost", "127.0.0.1"}:
        parser.error("LegalDesk loopback entry only accepts localhost/127.0.0.1")
    if not 1 <= args.port <= 65_535:
        parser.error("port must be between 1 and 65535")
    if args.port != 8000:
        parser.error("port must remain 8000 so the configured OAuth callback stays coherent")
    composition = build_aws_composition(allow_aws=args.allow_aws)
    application = create_http_app(composition)
    with make_server(args.host, args.port, application, handler_class=QuietRequestHandler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
