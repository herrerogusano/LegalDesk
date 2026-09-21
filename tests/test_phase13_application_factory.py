from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from legaldesk.application import AWSResourceConfig, build_aws_composition


class _Table:
    pass


class Phase13ApplicationFactoryTests(unittest.TestCase):
    def config(self) -> AWSResourceConfig:
        return AWSResourceConfig(
            region="eu-west-1",
            metadata_table_name="table",
            source_bucket_name="bucket",
            knowledge_base_id="kb-id",
            data_source_id="source-id",
            guardrail_identifier="guardrail-id",
            guardrail_version="1",
            resolver_model_id="resolver-model",
            writer_model_id="writer-model",
            harness_arn="arn:aws:bedrock-agentcore:eu-west-1:123:harness/test",
            gateway_url="https://gateway.example.test/mcp",
            memory_id="memory-id",
            jwks_url="https://issuer.example.test/.well-known/jwks.json",
            issuer="https://issuer.example.test",
            client_id="client-id",
            authorization_endpoint="https://issuer.example.test/oauth2/authorize",
            audience="audience",
            matter_catalog=("mat_sundial",),
            prompt_path=ROOT / "prompts" / "legaldesk-system.md",
            token_endpoint="https://issuer.example.test/oauth2/token",
        )

    def test_gate_happens_before_boto_or_jwks(self):
        with patch("boto3.client") as client, patch("boto3.resource") as resource, patch("legaldesk.application.PyJwtJwksKeyResolver") as resolver:
            with self.assertRaises(PermissionError):
                build_aws_composition(allow_aws=False, config=self.config())
        client.assert_not_called()
        resource.assert_not_called()
        resolver.assert_not_called()

    def test_factory_constructs_real_adapters_with_retry_disabled_config(self):
        table = _Table()
        fake_client = object()
        with patch("boto3.resource", return_value=type("Dynamo", (), {"Table": lambda self, name: table})()) as resource, patch("boto3.client", return_value=fake_client) as client, patch("legaldesk.application.PyJwtJwksKeyResolver", return_value=object()) as resolver:
            composition = build_aws_composition(allow_aws=True, config=self.config())
        self.assertEqual(composition.matter_catalog, ("mat_sundial",))
        self.assertIsNotNone(composition.chat_service)
        self.assertIsNotNone(composition.token_exchange)
        resolver.assert_called_once_with("https://issuer.example.test/.well-known/jwks.json")
        self.assertGreaterEqual(client.call_count, 4)
        for call in client.call_args_list:
            self.assertEqual(call.kwargs["config"].retries["total_max_attempts"], 1)
        resource.assert_called_once()

    def test_retrieval_and_ingestion_use_supported_distinct_bedrock_apis(self):
        """The SDK model, not a handwritten client name, defines the split."""
        from botocore.session import get_session

        session = get_session()
        runtime_model = session.get_service_model("bedrock-agent-runtime")
        control_model = session.get_service_model("bedrock-agent")
        self.assertIn("Retrieve", runtime_model.operation_names)
        self.assertNotIn("StartIngestionJob", runtime_model.operation_names)
        self.assertIn("StartIngestionJob", control_model.operation_names)
        self.assertIn("GetIngestionJob", control_model.operation_names)

        table = _Table()
        clients = {}
        instances = {}

        def make_client(service_name, **kwargs):
            clients[service_name] = kwargs
            instance = type("ProviderClient", (), {})()
            instances[service_name] = instance
            return instance

        observed = {}

        def spy_answer(*args, **kwargs):
            observed.update(kwargs)
            return {"operationStatus": "ok"}

        with patch("boto3.resource", return_value=type("Dynamo", (), {"Table": lambda self, name: table})()), patch("boto3.client", side_effect=make_client), patch("legaldesk.application.PyJwtJwksKeyResolver", return_value=object()), patch("legaldesk.chat.answer_question", side_effect=spy_answer):
            composition = build_aws_composition(allow_aws=True, config=self.config())
            composition.chat_service(object(), matter_id="matter", conversation_id="conversation", session_id="session", question="question", correlation_id="00000000-0000-4000-8000-000000000001")
        self.assertIsNotNone(composition.sync_service)
        self.assertIn("bedrock-agent-runtime", clients)
        self.assertIn("bedrock-agent", clients)
        self.assertIs(composition.sync_service.client, instances["bedrock-agent"])
        self.assertIsNot(instances["bedrock-agent-runtime"], instances["bedrock-agent"])
        self.assertIs(observed["retrieval_client"], instances["bedrock-agent-runtime"])
        self.assertIsNot(observed["retrieval_client"], composition.sync_service.client)
        for kwargs in clients.values():
            self.assertEqual(kwargs["config"].retries["total_max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
