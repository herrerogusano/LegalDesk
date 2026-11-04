"""F-04: retain closed exception evidence without logging provider/request text."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path[:0] = [str(ROOT / "agent/src"), str(ROOT / "backend/src"), str(ROOT / "tests")]

from botocore.exceptions import EventStreamError
from legaldesk_agent.client import (
    ClientStreamParserError, HarnessInvocationError, HarnessInvoker,
    _record_stream_error, invoke_agent_chat,
)
from test_phase15_agent_chat_client import HARNESS_ARN, PROMPT, StreamClient, make_scope


class StreamDiagnosticsTests(unittest.TestCase):
    def capture(self, call):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = call()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, [e for e in events if e.get("event_type") == "harness_stream_diagnostic"], output.getvalue()

    def provider_error(self, code="runtimeClientError", status=400):
        return EventStreamError({"Error": {"Code": code, "Message": "SECRET question/document/token required"},
                                 "ResponseMetadata": {"HTTPStatusCode": status,
                                                      "HTTPHeaders": {"authorization": "SECRET"}}}, "InvokeHarness")

    def test_cut_agent_stream_keeps_result_and_closed_provider_evidence(self):
        client = StreamClient([{"messageStart": {"role": "assistant"}}], error_after=(1, self.provider_error()))
        result, diagnostics, output = self.capture(lambda: invoke_agent_chat(
            HarnessInvoker(client, HARNESS_ARN), "synthetic", invocation_scope=make_scope(), system_prompt=PROMPT))
        self.assertEqual((result.loop_outcome, result.stream_error_hint), ("failed", "missing_required_argument"))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual({k: v for k, v in diagnostics[0].items() if k not in {"correlation_id", "event_type"}}, {
            "exception_class": "EventStreamError", "aws_error_code": "runtimeClientError",
            "sdk_status": 400, "events_received": 1,
        })
        self.assertNotIn("SECRET", output)

    def test_pre_event_retry_still_raises_the_original_exception(self):
        error = self.provider_error("InternalServerException", 500)
        client = StreamClient(error_after=(0, error))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(EventStreamError) as caught:
            invoke_agent_chat(HarnessInvoker(client, HARNESS_ARN), "synthetic",
                              invocation_scope=make_scope(), system_prompt=PROMPT)
        self.assertIs(caught.exception, error)
        self.assertIn('"events_received":0', output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())

    def test_simple_invoker_records_before_wrapping_the_sdk_error(self):
        error = self.provider_error()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(HarnessInvocationError) as caught:
            HarnessInvoker(StreamClient(error_after=(0, error)), HARNESS_ARN).invoke("synthetic")
        self.assertIs(caught.exception.__cause__, error)
        self.assertIn('"aws_error_code":"runtimeClientError"', output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())

    def test_unknown_fields_and_exception_names_never_escape(self):
        # A provider can put arbitrary request text into any string field.
        unknown = type("SECRET", (Exception,), {})("SECRET")
        unknown.response = {"Error": {"Code": "SECRET"}, "ResponseMetadata": {"HTTPStatusCode": "SECRET"}}
        _, diagnostics, output = self.capture(lambda: _record_stream_error(unknown, "safe", 0))
        self.assertEqual(diagnostics[0]["exception_class"], "other")
        self.assertEqual(diagnostics[0]["aws_error_code"], "other")
        self.assertIsNone(diagnostics[0]["sdk_status"])
        self.assertNotIn("SECRET", output)
        with patch("builtins.print", side_effect=OSError):
            _record_stream_error(unknown, "safe", 0)  # logging cannot change behavior

    def test_malformed_response_keeps_its_parser_error(self):
        class BadClient:
            def invoke_harness(self, **kwargs):
                return {"stream": None}

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ClientStreamParserError):
            HarnessInvoker(BadClient(), HARNESS_ARN).invoke("synthetic")


if __name__ == "__main__":
    unittest.main()
