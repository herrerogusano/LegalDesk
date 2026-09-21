"""Offline checks of the one-shot operator's security-relevant helpers."""
import json
import tempfile
import unittest
from pathlib import Path
from phase13_live_smoke import (
    ACCOUNT,
    BUCKET,
    HARNESS,
    REGION,
    TENANT,
    TrackingSession,
    make_resource_config,
    offline_factory_preflight,
    resolve_report_path,
    session_policy,
)


class LivePreflightTests(unittest.TestCase):
    def test_application_policy_is_restricted_and_fits_sts(self):
        policy = session_policy(["a" * 36, "b" * 36])
        self.assertLessEqual(len(policy), 2048)
        statements = json.loads(policy)["Statement"]
        self.assertTrue(all(item["Effect"] == "Allow" and item["Resource"] != "*" for item in statements))
        s3 = next(item for item in statements if "s3:PutObject" in item["Action"])
        self.assertEqual(s3["Resource"], f"arn:aws:s3:::{BUCKET}/tenants/{TENANT}/matters/*/documents/*")
        dynamo = next(item for item in statements if "dynamodb:Query" in item["Action"])
        self.assertNotIn("dynamodb:Scan", dynamo["Action"])
        self.assertIn("ForAllValues:StringLike", dynamo["Condition"])
        self.assertFalse(any("iam:" in json.dumps(item) or "CreateUser" in json.dumps(item) for item in statements))
        harness = next(item for item in statements if "bedrock-agentcore:InvokeHarness" in item["Action"])
        self.assertEqual(harness["Resource"], HARNESS)

    def test_session_wraps_only_explicitly_supplied_scoped_provider(self):
        class Provider:
            def __init__(self):
                self.calls = []
            def client(self, service, **kwargs):
                self.calls.append(service)
                return self
            def put_object(self, **kwargs):
                return {"ok": True}
        provider = Provider()
        state = {"objects": set(), "keys": set(), "memory_scopes": set()}
        session = TrackingSession(provider, state)
        self.assertEqual(session.client("s3").put_object(Key=f"tenants/{TENANT}/test"), {"ok": True})
        self.assertEqual(provider.calls, ["s3"])
        self.assertEqual(state["objects"], {f"tenants/{TENANT}/test"})

    def test_exact_resource_config_builds_offline_with_access_only_jwt(self):
        config = make_resource_config()
        self.assertIsNone(config.audience)
        result = offline_factory_preflight(config)
        self.assertEqual(result["allowedTokenUse"], ("access",))
        self.assertEqual(result["audienceConfigured"], False)
        self.assertEqual(
            set(result["providerClientConstructors"]),
            {"s3", "bedrock-runtime", "bedrock-agent-runtime", "bedrock-agent", "bedrock-agentcore"},
        )

    def test_new_report_path_is_exclusive_and_cannot_reuse_history(self):
        with self.assertRaises(RuntimeError):
            resolve_report_path("fresh-20260922", "build/phase13-smoke/live-result.json")
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "live-result-fresh-20260922.json"
            existing.write_text("sentinel", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                resolve_report_path("fresh-20260922", existing)


if __name__ == "__main__":
    unittest.main()
