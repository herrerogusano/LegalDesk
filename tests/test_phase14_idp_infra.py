from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-idp.yaml"


class _CloudFormationLoader(yaml.SafeLoader):
    """Parse intrinsic tags into their CloudFormation long-form equivalents."""


def _ref(loader: _CloudFormationLoader, node: yaml.Node) -> dict[str, Any]:
    return {"Ref": loader.construct_scalar(node)}


def _get_att(loader: _CloudFormationLoader, node: yaml.Node) -> dict[str, Any]:
    return {"Fn::GetAtt": loader.construct_scalar(node).split(".", 1)}


def _intrinsic(name: str):
    def constructor(loader: _CloudFormationLoader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {name: value}

    return constructor


_CloudFormationLoader.add_constructor("!Ref", _ref)
_CloudFormationLoader.add_constructor("!GetAtt", _get_att)
for _tag, _name in (("!Sub", "Fn::Sub"), ("!If", "Fn::If"), ("!Equals", "Fn::Equals"), ("!And", "Fn::And")):
    _CloudFormationLoader.add_constructor(_tag, _intrinsic(_name))


def _actions(statement: dict[str, Any]) -> set[str]:
    value = statement.get("Action", [])
    return {value} if isinstance(value, str) else set(value)


def _statements(template: dict[str, Any], resource_name: str) -> list[dict[str, Any]]:
    policies = template["Resources"][resource_name]["Properties"].get("Policies", [])
    statements: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict) and "Fn::If" in value:
            branches = value["Fn::If"]
            if isinstance(branches, list) and len(branches) == 3:
                add(branches[1])
            return
        if isinstance(value, dict):
            statements.append(value)

    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            add(statement)
    return statements


def _statement(template: dict[str, Any], resource_name: str, sid: str) -> dict[str, Any]:
    matches = [item for item in _statements(template, resource_name) if item.get("Sid") == sid]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {sid} statement, got {len(matches)}")
    return matches[0]


class Phase14IDPInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CloudFormationLoader)
        cls.resources = cls.template["Resources"]

    def test_reuses_existing_boundaries_and_does_not_create_unapproved_resources(self) -> None:
        parameters = self.template["Parameters"]
        for name in (
            "MetadataTableArn", "SourceBucketName", "SourceBucketArn", "ArtifactBucketName",
            "ExistingUserPoolId", "ExistingUserPoolDomain", "ExistingGatewayArn",
        ):
            self.assertIn(name, parameters)
        forbidden = {
            "AWS::S3::Bucket", "AWS::DynamoDB::Table", "AWS::StepFunctions::StateMachine",
            "AWS::BedrockAgentCore::Gateway", "AWS::BedrockAgentCore::GatewayTarget",
            "AWS::SecretsManager::Secret", "AWS::KMS::Key", "AWS::CloudWatch::Alarm",
        }
        self.assertTrue(forbidden.isdisjoint({resource["Type"] for resource in self.resources.values()}))
        self.assertNotIn("dynamodb:Scan", str(self.template))

    def test_queues_mappings_and_disabled_function_bounds_are_executable_contracts(self) -> None:
        self.assertEqual(
            {name for name, resource in self.resources.items() if resource["Type"] == "AWS::SQS::Queue"},
            {"IDPWorkDeadLetterQueue", "IDPWorkQueue", "IDPOCRDeadLetterQueue", "IDPOCRCompletionQueue"},
        )
        self.assertEqual(self.template["Parameters"]["EnableIDPProcessing"]["Default"], "false")
        self.assertEqual(self.template["Parameters"]["IDPMaxCallsPerRun"]["Default"], 2)
        self.assertEqual(self.template["Parameters"]["IDPMaxCallsPerRun"]["AllowedValues"], [1, 2])
        self.assertEqual(self.template["Parameters"]["IDPReadTimeoutSeconds"]["Default"], 15)
        self.assertEqual(self.template["Parameters"]["IDPReadTimeoutSeconds"]["MaxValue"], 15)
        self.assertEqual(self.template["Parameters"]["IDPModelReadTimeoutSeconds"]["Default"], 120)
        self.assertEqual(self.template["Parameters"]["IDPModelReadTimeoutSeconds"]["MinValue"], 90)
        self.assertEqual(self.template["Parameters"]["IDPModelReadTimeoutSeconds"]["MaxValue"], 120)
        self.assertEqual(self.template["Parameters"]["IDPMaxOCRApiCalls"]["Default"], 4)
        self.assertEqual(self.template["Parameters"]["IDPMaxOCRApiCalls"]["MaxValue"], 4)
        self.assertEqual(self.template["Parameters"]["IDPGlobalDeadlineSeconds"]["Default"], 330)
        self.assertEqual(self.template["Parameters"]["IDPClaimLeaseSeconds"]["Default"], 480)
        for name in ("IDPWorkQueue", "IDPOCRCompletionQueue"):
            props = self.resources[name]["Properties"]
            self.assertEqual(props["VisibilityTimeout"], 2160)
            self.assertEqual(props["RedrivePolicy"]["maxReceiveCount"], 3)
        for name in ("IDPWorkerFunction", "IDPOCRContinuationFunction"):
            props = self.resources[name]["Properties"]
            self.assertEqual(props["Timeout"], 360)
            self.assertEqual(props["ReservedConcurrentExecutions"], {"Fn::If": ["IDPEnabled", {"Ref": "AWS::NoValue"}, 0]})
            variables = props["Environment"]["Variables"]
            self.assertEqual(variables["LEGALDESK_IDP_MAX_CALLS_PER_RUN"], {"Ref": "IDPMaxCallsPerRun"})
            self.assertEqual(variables["LEGALDESK_IDP_READ_TIMEOUT_SECONDS"], {"Ref": "IDPReadTimeoutSeconds"})
            self.assertEqual(variables["LEGALDESK_IDP_MODEL_READ_TIMEOUT_SECONDS"], {"Ref": "IDPModelReadTimeoutSeconds"})
            self.assertEqual(variables["LEGALDESK_IDP_MAX_OCR_API_CALLS"], {"Ref": "IDPMaxOCRApiCalls"})
            self.assertEqual(variables["LEGALDESK_IDP_GLOBAL_DEADLINE_SECONDS"], {"Ref": "IDPGlobalDeadlineSeconds"})
            self.assertEqual(variables["LEGALDESK_IDP_CLAIM_LEASE_SECONDS"], {"Ref": "IDPClaimLeaseSeconds"})
        mappings = [resource for resource in self.resources.values() if resource["Type"] == "AWS::Lambda::EventSourceMapping"]
        self.assertEqual(len(mappings), 2)
        for mapping in mappings:
            self.assertEqual(mapping["Condition"], "IDPEnabled")
            props = mapping["Properties"]
            self.assertTrue(props["Enabled"])
            self.assertEqual(props["BatchSize"], 1)
            self.assertEqual(props["FunctionResponseTypes"], ["ReportBatchItemFailures"])
            self.assertEqual(props["ScalingConfig"]["MaximumConcurrency"], 2)
        self.assertGreaterEqual(2160, 6 * 360)
        # The runtime must enforce the global wall-clock deadline and lease;
        # this static contract does not pretend provider-call sums are a hard
        # end-to-end bound because S3/Dynamo persistence adds variable work.
        self.assertLess(330, 360)
        self.assertGreater(480, 360)
        self.assertLessEqual(4 * 15 + 2 * 120, 330)

    def test_lambda_roles_cover_polling_and_continuation_queue_actions(self) -> None:
        worker = _statement(self.template, "IDPWorkerExecutionRole", "ConsumeAndRedeliverWorkMessages")
        self.assertEqual(_actions(worker), {"sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage"})
        self.assertEqual(worker["Resource"], {"Fn::GetAtt": ["IDPWorkQueue", "Arn"]})
        ocr = _statement(self.template, "IDPOCRContinuationExecutionRole", "ConsumeOCRAndContinueWork")
        self.assertEqual(_actions(ocr), {"sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"})
        self.assertEqual(ocr["Resource"], {"Fn::GetAtt": ["IDPOCRCompletionQueue", "Arn"]})
        continuation = _statement(self.template, "IDPOCRContinuationExecutionRole", "EnqueueBoundedWorkContinuation")
        self.assertEqual(_actions(continuation), {"sqs:SendMessage"})
        self.assertEqual(continuation["Resource"], {"Fn::GetAtt": ["IDPWorkQueue", "Arn"]})

    def test_textract_channel_and_roles_keep_publish_and_api_scopes_separate(self) -> None:
        topic = self.resources["TextractCompletionTopic"]["Properties"]
        self.assertEqual(topic["TopicName"], {"Fn::Sub": "AmazonTextract-${AWS::StackName}"})
        subscription = self.resources["TextractCompletionSubscription"]["Properties"]
        self.assertEqual(subscription["Protocol"], "sqs")
        self.assertEqual(subscription["Endpoint"], {"Fn::GetAtt": ["IDPOCRCompletionQueue", "Arn"]})
        topic_policy = self.resources["TextractCompletionTopicPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
        self.assertEqual(_actions(topic_policy), {"sns:Publish"})
        self.assertEqual(topic_policy["Resource"], {"Ref": "TextractCompletionTopic"})
        self.assertEqual(topic_policy["Condition"]["StringEquals"]["aws:SourceAccount"], {"Ref": "AWS::AccountId"})
        self.assertIn("eu-west-1", topic_policy["Condition"]["ArnLike"]["aws:SourceArn"]["Fn::Sub"])
        trust = self.resources["TextractNotificationRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        self.assertEqual(trust["Principal"], {"Service": "textract.amazonaws.com"})
        self.assertIn("aws:SourceAccount", trust["Condition"]["StringEquals"])
        self.assertIn("aws:SourceArn", trust["Condition"]["ArnLike"])
        publish = _statement(self.template, "TextractNotificationRole", "PublishOnlyThisTextractCompletionTopic")
        self.assertEqual(_actions(publish), {"sns:Publish"})
        self.assertEqual(publish["Resource"], {"Ref": "TextractCompletionTopic"})
        self.assertNotIn("Condition", publish)

    def test_textract_exception_and_passrole_are_exactly_scoped(self) -> None:
        start = _statement(self.template, "IDPWorkerExecutionRole", "StartTextractOnlyInIreland")
        self.assertEqual(_actions(start), {"textract:StartDocumentTextDetection"})
        self.assertEqual(start["Resource"], "*")
        self.assertEqual(start["Condition"]["StringEquals"]["aws:RequestedRegion"], "eu-west-1")
        get = _statement(self.template, "IDPOCRContinuationExecutionRole", "ReadTextractResultsOnlyInIreland")
        self.assertEqual(_actions(get), {"textract:GetDocumentTextDetection"})
        self.assertEqual(get["Resource"], "*")
        self.assertEqual(get["Condition"]["StringEquals"]["aws:RequestedRegion"], "eu-west-1")
        passrole = _statement(self.template, "IDPWorkerExecutionRole", "PassOnlyTextractNotificationRole")
        self.assertEqual(_actions(passrole), {"iam:PassRole"})
        self.assertEqual(passrole["Resource"], {"Fn::GetAtt": ["TextractNotificationRole", "Arn"]})
        self.assertEqual(passrole["Condition"]["StringEquals"]["iam:PassedToService"], "textract.amazonaws.com")

    def test_data_plane_policy_uses_actual_idp_keys_and_canonical_objects(self) -> None:
        expected_leading = {"TENANT#${BetaTenantId}#MATTER#*", "IDP#JOB#*", "IDP#OCRJOB#*"}
        for role_name, sid in (("IDPWorkerExecutionRole", "ReadWriteIDPStateOnly"), ("IDPOCRContinuationExecutionRole", "ReadWriteIDPOCRStateOnly")):
            state = _statement(self.template, role_name, sid)
            keys = {
                item["Fn::Sub"] if isinstance(item, dict) and "Fn::Sub" in item else item
                for item in state["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
            }
            self.assertEqual(keys, expected_leading)
            canonical = _statement(self.template, role_name, "ReadCanonicalBetaDocuments")
            self.assertEqual(_actions(canonical), {"s3:GetObject"})
            self.assertEqual(
                {item["Fn::Sub"] for item in canonical["Resource"]},
                {
                    "${SourceBucketArn}/tenants/${BetaTenantId}/matters/*/documents/*/original.pdf",
                    "${SourceBucketArn}/tenants/${BetaTenantId}/matters/*/documents/*/original.txt",
                },
            )
            artifacts = _statement(self.template, role_name, "StoreOnlyIDPArtifactsOutsideRAG")
            self.assertEqual(set(artifacts["Action"]), {"s3:GetObject", "s3:PutObject"})
            self.assertEqual(artifacts["Resource"]["Fn::Sub"], "${SourceBucketArn}/idp-artifacts/tenant=${BetaTenantId}/matter=*/document=*/run=*/*")
            all_actions = {action for statement in _statements(self.template, role_name) for action in _actions(statement)}
            self.assertNotIn("s3:DeleteObject", all_actions)

    def test_model_scope_env_contract_machine_identity_and_no_direct_privilege(self) -> None:
        resources = self.resources
        model_arns = {
            item.get("Ref") if isinstance(item, dict) else item
            for item in _statement(self.template, "IDPWorkerExecutionRole", "InvokeApprovedEuropeanSonnetProfile")["Resource"]
        }
        self.assertIn("IDPInferenceProfileArn", model_arns)
        self.assertIn("IDPFoundationModelArn", model_arns)
        for region in ("eu-central-1", "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-3"):
            self.assertIn(f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-6", model_arns)
        for function_name in ("IDPWorkerFunction", "IDPOCRContinuationFunction"):
            variables = resources[function_name]["Properties"]["Environment"]["Variables"]
            for name in ("LEGALDESK_IDP_MODEL_ID", "LEGALDESK_IDP_PROMPT_VERSION", "LEGALDESK_IDP_OCR_SNS_TOPIC_ARN", "LEGALDESK_IDP_OCR_ROLE_ARN", "LEGALDESK_IDP_OCR_SQS_SOURCE_ARN"):
                self.assertIn(name, variables)
        all_actions = {action for role in ("IDPWorkerExecutionRole", "IDPOCRContinuationExecutionRole") for statement in _statements(self.template, role) for action in _actions(statement)}
        self.assertNotIn("lambda:InvokeFunction", all_actions)
        self.assertNotIn("lambda:InvokeFunctionUrl", all_actions)
        self.assertNotIn("bedrock-agentcore:InvokeGateway", all_actions)
        self.assertNotIn("GATEWAY#GRANT", str(self.template))

    def test_machine_identity_is_review_creation_only_and_secret_is_not_output(self) -> None:
        resources = self.resources
        resource_server = resources["IDPResourceServer"]["Properties"]
        self.assertEqual(resource_server["Identifier"], {"Ref": "IDPResourceServerIdentifier"})
        self.assertEqual(resource_server["Scopes"][0]["ScopeName"], {"Ref": "IDPReviewScopeName"})
        client = resources["IDPMachineClient"]["Properties"]
        self.assertTrue(client["GenerateSecret"])
        self.assertEqual(client["AllowedOAuthFlows"], ["client_credentials"])
        self.assertEqual(client["AllowedOAuthScopes"], [{"Fn::Sub": "${IDPResourceServerIdentifier}/${IDPReviewScopeName}"}])
        self.assertEqual(client["AccessTokenValidity"], 5)
        self.assertEqual(client["TokenValidityUnits"], {"AccessToken": "minutes"})
        ssm = _statement(self.template, "IDPWorkerExecutionRole", "ReadOperatorBootstrappedM2MSecretParameter")
        self.assertEqual(_actions(ssm), {"ssm:GetParameter"})
        self.assertEqual(ssm["Resource"], {"Ref": "M2MSecretParameterArn"})
        self.assertNotIn("M2MSecretParameter", self.template.get("Outputs", {}))
        self.assertNotIn("AWS::SecretsManager::Secret", {resource["Type"] for resource in resources.values()})


if __name__ == "__main__":
    unittest.main()
