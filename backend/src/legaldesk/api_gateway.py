"""Strict API Gateway HTTP API v2 adapter for the public application boundary.

The adapter is deliberately separate from the loopback WSGI entry point.  It
constructs the AWS composition only after a syntactically valid dynamic event
has passed the boundary checks, and it never substitutes local providers when
composition fails.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from io import BytesIO
from threading import Lock
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl

from .application import AWSResourceConfig, build_aws_composition
from .http_app import MAX_HTTP_BODY, TRUSTED_EDGE_ENVIRON, TRUSTED_EDGE_HEADER, create_http_app


_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAX_HEADERS = 64
_MAX_HEADER_VALUE = 8_192
_MAX_PATH = 4_096
_MAX_QUERY = 8_192
_PUBLIC_DYNAMIC_PREFIX = "/api/"
_app: Callable[..., Any] | None = None
_app_lock = Lock()


class APIGatewayEventError(ValueError):
    """Raised for malformed or unsupported API Gateway events."""


class TrustedEdgeDenied(APIGatewayEventError):
    """Raised when a request did not arrive through the approved edge."""


def _error(status: int, code: str) -> dict[str, object]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps({"error": code}, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def _header_map(event: Mapping[str, object]) -> dict[str, str]:
    raw = event.get("headers", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise APIGatewayEventError("headers must be an object")
    if len(raw) > _MAX_HEADERS:
        raise APIGatewayEventError("too many headers")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _HEADER_NAME.fullmatch(key):
            raise APIGatewayEventError("header name is invalid")
        if not isinstance(value, str) or len(value) > _MAX_HEADER_VALUE or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise APIGatewayEventError("header value is invalid")
        lowered = key.lower()
        if lowered in result:
            raise APIGatewayEventError("duplicate header")
        result[lowered] = value
    return result


def _body_bytes(event: Mapping[str, object], headers: Mapping[str, str]) -> bytes:
    encoded = event.get("isBase64Encoded", False)
    if type(encoded) is not bool:
        raise APIGatewayEventError("isBase64Encoded must be boolean")
    body = event.get("body")
    if body is None:
        payload = b""
    elif not isinstance(body, str):
        raise APIGatewayEventError("body must be a string")
    elif encoded:
        try:
            payload = base64.b64decode(body.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise APIGatewayEventError("body encoding is invalid") from exc
    else:
        payload = body.encode("utf-8")
    if len(payload) > MAX_HTTP_BODY:
        raise APIGatewayEventError("body too large")
    content_length = headers.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal() or int(content_length) != len(payload):
            raise APIGatewayEventError("content length is invalid")
    return payload


def _validated_request(event: Mapping[str, object], *, trusted_edge_value: str | None = None) -> tuple[dict[str, Any], str]:
    if event.get("version") != "2.0":
        raise APIGatewayEventError("only HTTP API v2 is supported")
    if event.get("multiValueHeaders") not in (None, {}):
        raise APIGatewayEventError("multi-value headers are unsupported")
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        raise APIGatewayEventError("requestContext is required")
    http = request_context.get("http")
    if not isinstance(http, Mapping):
        raise APIGatewayEventError("requestContext.http is required")
    method = http.get("method")
    if not isinstance(method, str) or not method or method.upper() != method or not re.fullmatch(r"[A-Z]+", method):
        raise APIGatewayEventError("method is invalid")
    path = event.get("rawPath", http.get("path"))
    if not isinstance(path, str) or not path.startswith("/") or len(path) > _MAX_PATH or any(char in path for char in "\r\n"):
        raise APIGatewayEventError("path is invalid")
    if "?" in path or "/../" in f"{path}/" or path.endswith("/..") or "/./" in f"{path}/" or path.endswith("/."):
        raise APIGatewayEventError("path is invalid")
    context_path = http.get("path")
    if context_path is not None and (not isinstance(context_path, str) or context_path != path):
        raise APIGatewayEventError("path mismatch")
    if path not in {"/login", "/callback", "/logout"} and not (path == "/api" or path.startswith(_PUBLIC_DYNAMIC_PREFIX)):
        raise APIGatewayEventError("path is not a public API route")
    raw_query = event.get("rawQueryString", "")
    if raw_query is None:
        raw_query = ""
    if not isinstance(raw_query, str) or len(raw_query) > _MAX_QUERY or any(char in raw_query for char in "\r\n") or re.search(r"%(?![0-9A-Fa-f]{2})", raw_query):
        raise APIGatewayEventError("query is invalid")
    try:
        parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=128)
    except (TypeError, ValueError) as exc:
        raise APIGatewayEventError("query is invalid") from exc
    query_parameters = event.get("queryStringParameters")
    if query_parameters is not None:
        if not isinstance(query_parameters, Mapping) or len(query_parameters) > 128:
            raise APIGatewayEventError("query parameters are invalid")
        if any(
            not isinstance(key, str)
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in key)
            or not isinstance(value, (str, type(None)))
            for key, value in query_parameters.items()
        ):
            raise APIGatewayEventError("query parameters are invalid")
    headers = _header_map(event)
    cookies = event.get("cookies")
    if cookies is not None:
        if not isinstance(cookies, list) or not cookies or any(not isinstance(item, str) or not item or len(item) > _MAX_HEADER_VALUE or any(char in item for char in "\r\n") for item in cookies):
            raise APIGatewayEventError("cookies are invalid")
        if "cookie" in headers:
            raise APIGatewayEventError("duplicate cookie header")
        cookie_header = "; ".join(cookies)
    else:
        cookie_header = headers.get("cookie", "")
    host = headers.get("host")
    if not host or any(char.isspace() or char in ",/\\" or ord(char) < 0x20 or ord(char) == 0x7F for char in host):
        raise APIGatewayEventError("host is required")
    if trusted_edge_value is not None:
        marker = headers.get(TRUSTED_EDGE_HEADER.lower())
        if not isinstance(marker, str) or not hmac.compare_digest(marker, trusted_edge_value):
            raise TrustedEdgeDenied("trusted edge marker is invalid")
    body = _body_bytes(event, headers)
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": raw_query,
        "HTTP_HOST": host,
        "HTTP_COOKIE": cookie_header,
        "wsgi.input": BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
        "SERVER_NAME": host.split(":", 1)[0],
        "SERVER_PORT": "443",
        "wsgi.url_scheme": "https",
        "wsgi.version": (1, 0),
        "wsgi.errors": BytesIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
        "legaldesk.edge_marker": headers.get(TRUSTED_EDGE_HEADER.lower()),
    }
    for name, value in headers.items():
        if name in {"host", "cookie", "content-length"}:
            continue
        if name == TRUSTED_EDGE_HEADER.lower():
            # The marker authenticates the edge-to-origin hop only.  It is
            # never exposed as application business input.
            continue
        environ[f"HTTP_{name.upper().replace('-', '_')}"] = value
    content_type = headers.get("content-type")
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    if trusted_edge_value is not None:
        environ[TRUSTED_EDGE_ENVIRON] = True
    return environ, path


def _verify_trusted_edge(environ: dict[str, Any], expected: str) -> None:
    marker = environ.pop("legaldesk.edge_marker", None)
    if not isinstance(marker, str) or not hmac.compare_digest(marker, expected):
        raise TrustedEdgeDenied("trusted edge marker is invalid")
    environ[TRUSTED_EDGE_ENVIRON] = True


def _invoke_wsgi(application: Callable[..., Any], environ: dict[str, Any]) -> dict[str, object]:
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]], _exc_info: object = None) -> None:
        if captured:
            raise RuntimeError("response started twice")
        if not isinstance(status, str) or not re.fullmatch(r"[1-5][0-9]{2} [^\r\n]+", status):
            raise RuntimeError("response status is invalid")
        normalized: dict[str, str] = {}
        cookies: list[str] = []
        for name, value in headers:
            if not isinstance(name, str) or not isinstance(value, str) or any(char in value for char in "\r\n"):
                raise RuntimeError("response header is invalid")
            if name.lower() == "set-cookie":
                cookies.append(value)
                continue
            key = name.lower()
            if key in normalized:
                raise RuntimeError("duplicate response header")
            normalized[key] = value
        captured.update({"status": int(status[:3]), "headers": normalized, "cookies": cookies})

    result = application(environ, start_response)
    if not captured or not isinstance(result, (list, tuple)):
        raise RuntimeError("WSGI response is invalid")
    body = b"".join(item if isinstance(item, bytes) else bytes(item) for item in result)
    if len(body) > MAX_HTTP_BODY:
        raise RuntimeError("response too large")
    headers = dict(captured["headers"])
    headers.setdefault("content-type", "application/json; charset=utf-8")
    headers["content-length"] = str(len(body))
    response: dict[str, object] = {
        "statusCode": captured["status"],
        "headers": headers,
        "body": body.decode("utf-8"),
        "isBase64Encoded": False,
    }
    if captured["cookies"]:
        response["cookies"] = captured["cookies"]
    return response


def _application(config: AWSResourceConfig | None = None) -> Callable[..., Any]:
    global _app
    if _app is None:
        with _app_lock:
            if _app is None:
                config = config or AWSResourceConfig.from_environment()
                if config.public_mode is not True:
                    raise RuntimeError("public mode is required")
                composition = build_aws_composition(allow_aws=True, config=config)
                if composition.public_mode is not True or not composition.trusted_edge_value:
                    raise RuntimeError("public composition is not configured")
                _app = create_http_app(composition)
    return _app


def lambda_handler(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    try:
        if not isinstance(event, Mapping):
            raise APIGatewayEventError("event must be an object")
        environ, _path = _validated_request(event)
        if _app is None:
            config = AWSResourceConfig.from_environment()
            if config.public_mode is not True or not config.trusted_edge_value:
                raise RuntimeError("public mode is required")
            _verify_trusted_edge(environ, config.trusted_edge_value)
            return _invoke_wsgi(_application(config), environ)
        application = _application()
        composition = getattr(application, "composition", None)
        if composition is None or composition.public_mode is not True or not composition.trusted_edge_value:
            raise RuntimeError("public composition is not configured")
        _verify_trusted_edge(environ, composition.trusted_edge_value)
        return _invoke_wsgi(application, environ)
    except TrustedEdgeDenied:
        return _error(403, "access_denied")
    except APIGatewayEventError:
        return _error(400, "invalid_request")
    except Exception:
        return _error(500, "operation_failed")


def reset_application_for_tests() -> None:
    global _app
    with _app_lock:
        _app = None


__all__ = ["APIGatewayEventError", "lambda_handler", "reset_application_for_tests"]
