from __future__ import annotations

from pathlib import Path
import unittest

import yaml


class _CloudFormationLoader(yaml.SafeLoader):
    """Parse CloudFormation intrinsics without resolving deployment values."""


def _intrinsic(loader: _CloudFormationLoader, tag: str, node: yaml.Node):
    name = tag.lstrip("!")
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {name: value}


for _name in ("Ref", "GetAtt", "Sub", "If", "Equals", "Not", "And"):
    _CloudFormationLoader.add_constructor(f"!{_name}", lambda loader, node, name=_name: _intrinsic(loader, name, node))


ROOT = Path(__file__).parents[1]


def _load(name: str) -> dict:
    with (ROOT / "infra" / "cloudformation" / name).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_CloudFormationLoader)


def _resources(template: dict) -> dict:
    return template["Resources"]


def _statements(template: dict, logical_id: str) -> list[dict]:
    role = _resources(template)[logical_id]
    statements: list[dict] = []
    for policy in role["Properties"]["Policies"]:
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _conditional_statements(template: dict, logical_id: str) -> list[dict]:
    values = []
    for statement in _statements(template, logical_id):
        if isinstance(statement, dict) and "If" in statement:
            values.append(statement["If"])
    return values


def _named_statement(template: dict, logical_id: str, sid: str) -> dict:
    for statement in _statements(template, logical_id):
        if isinstance(statement, dict) and statement.get("Sid") == sid:
            return statement
    raise AssertionError(f"missing {logical_id}/{sid}")


def _sub_values(values: list[object]) -> set[object]:
    return {value.get("Sub") if isinstance(value, dict) and "Sub" in value else value for value in values}


class Phase14ProducerRecoveryIaCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.security = _load("phase-14-document-security.yaml")
        cls.reconciliation = _load("phase-14-reconciliation.yaml")

    def test_opt_in_defaults_preserve_existing_deployed_contracts(self) -> None:
        for template in (self.security, self.reconciliation):
            params = template["Parameters"]
            self.assertEqual(params["EnableIDPProcessing"]["Default"], "false")
            self.assertEqual(template["Conditions"]["IDPEnabled"], {"Equals": [{"Ref": "EnableIDPProcessing"}, "true"]})
            self.assertEqual(params["IDPWorkQueueUrl"]["Default"], "")
            self.assertEqual(params["IDPWorkQueueArn"]["Default"], "arn:aws:sqs:eu-west-1:000000000000:disabled-idp-work")
            self.assertEqual(params["IDPModelId"]["Default"], "")
            self.assertEqual(params["IDPPromptVersion"]["Default"], "")

        # These changes are conditional policy branches, not new always-on
        # producers, schedules, functions, alarms or data stores.
        security_types = {value["Type"] for value in _resources(self.security).values()}
        reconciliation_types = {value["Type"] for value in _resources(self.reconciliation).values()}
        self.assertEqual(security_types, {
            "AWS::GuardDuty::MalwareProtectionPlan", "AWS::IAM::Role", "AWS::Logs::LogGroup",
            "AWS::SQS::Queue", "AWS::SQS::QueuePolicy", "AWS::Lambda::Function",
            "AWS::Events::Rule", "AWS::Lambda::Permission",
        })
        self.assertEqual(reconciliation_types, {
            "AWS::Logs::LogGroup", "AWS::SQS::Queue", "AWS::SQS::QueuePolicy",
            "AWS::IAM::Role", "AWS::Lambda::Function", "AWS::Events::Rule", "AWS::Lambda::Permission",
        })

    def test_malware_producer_uses_exact_conditional_job_and_queue_permissions(self) -> None:
        conditional = _conditional_statements(self.security, "MalwareScanExecutionRole")
        by_sid = {branch[1]["Sid"]: branch for branch in conditional}
        self.assertEqual(set(by_sid), {"CreateAndEnqueueIDPJob", "EnqueueIDPJobOnly"})
        for branch in by_sid.values():
            self.assertEqual(branch[0], "IDPEnabled")
            self.assertEqual(branch[2], {"Ref": "AWS::NoValue"})

        dynamo = by_sid["CreateAndEnqueueIDPJob"][1]
        self.assertEqual(set(dynamo["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"})
        self.assertEqual(dynamo["Resource"], {"Ref": "MetadataTableArn"})
        self.assertEqual(_sub_values(dynamo["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]), {
            "TENANT#${BetaTenantId}#MATTER#*", "IDP#JOB#*",
        })

        queue = by_sid["EnqueueIDPJobOnly"][1]
        self.assertEqual(queue["Action"], "sqs:SendMessage")
        self.assertEqual(queue["Resource"], {"Ref": "IDPWorkQueueArn"})

        variables = _resources(self.security)["MalwareScanFunction"]["Properties"]["Environment"]["Variables"]
        expected = {
            "LEGALDESK_IDP_ENABLED": {"Ref": "EnableIDPProcessing"},
            "LEGALDESK_IDP_QUEUE_URL": {"Ref": "IDPWorkQueueUrl"},
            "LEGALDESK_IDP_QUEUE_ARN": {"Ref": "IDPWorkQueueArn"},
            "LEGALDESK_IDP_MODEL_ID": {"Ref": "IDPModelId"},
            "LEGALDESK_IDP_PROMPT_VERSION": {"Ref": "IDPPromptVersion"},
        }
        for name, value in expected.items():
            self.assertEqual(variables[name], value)
        self.assertNotIn("GATEWAY#GRANT", str(dynamo))
        existing = _named_statement(self.security, "MalwareScanExecutionRole", "ReadAndTransitionDocumentMetadata")
        self.assertEqual(set(existing["Action"]), {"dynamodb:GetItem", "dynamodb:UpdateItem"})
        self.assertEqual(_sub_values(existing["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]), {"TENANT#${BetaTenantId}#MATTER#*"})

    def test_reconciliation_recovery_is_bounded_and_does_not_expand_gateway_scope(self) -> None:
        conditional = _conditional_statements(self.reconciliation, "ReconciliationExecutionRole")
        by_sid = {branch[1]["Sid"]: branch for branch in conditional}
        self.assertEqual(set(by_sid), {
            "RecoverIDPDispatchForCleanDocuments",
            "ReadCanonicalCleanDocumentsForIDPRecovery",
            "EnqueueRecoveredIDPJobsOnly",
            "PersistIDPReviewInvocationsOnly",
            "ReadOperatorBootstrappedM2MSecretParameter",
        })
        for sid, branch in by_sid.items():
            expected_condition = "IDPReviewEnabled" if sid in {"PersistIDPReviewInvocationsOnly", "ReadOperatorBootstrappedM2MSecretParameter"} else "IDPEnabled"
            self.assertEqual(branch[0], expected_condition)
            self.assertEqual(branch[2], {"Ref": "AWS::NoValue"})

        dynamo = by_sid["RecoverIDPDispatchForCleanDocuments"][1]
        self.assertEqual(set(dynamo["Action"]), {
            "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
        })
        self.assertEqual(dynamo["Resource"], {"Ref": "MetadataTableArn"})
        self.assertEqual(_sub_values(dynamo["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]), {
            "TENANT#${BetaTenantId}#MATTER#*", "IDP#JOB#*",
        })

        s3 = by_sid["ReadCanonicalCleanDocumentsForIDPRecovery"][1]
        self.assertEqual(s3["Action"], "s3:GetObject")
        self.assertEqual({item["Sub"] for item in s3["Resource"]}, {
            "${SourceBucketArn}/tenants/${BetaTenantId}/matters/*/documents/*/original.pdf",
            "${SourceBucketArn}/tenants/${BetaTenantId}/matters/*/documents/*/original.txt",
        })
        queue = by_sid["EnqueueRecoveredIDPJobsOnly"][1]
        self.assertEqual(queue["Action"], "sqs:SendMessage")
        self.assertEqual(queue["Resource"], {"Ref": "IDPWorkQueueArn"})
        invocation = by_sid["PersistIDPReviewInvocationsOnly"][1]
        self.assertEqual(set(invocation["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"})
        self.assertEqual(invocation["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"], ["GATEWAY#IDP-INVOCATION#*"])
        secret = by_sid["ReadOperatorBootstrappedM2MSecretParameter"][1]
        self.assertEqual(secret["Action"], "ssm:GetParameter")
        self.assertEqual(secret["Resource"], {"Ref": "M2MSecretParameterArn"})

        variables = _resources(self.reconciliation)["ReconciliationFunction"]["Properties"]["Environment"]["Variables"]
        self.assertEqual(variables["LEGALDESK_IDP_ENABLED"], {"Ref": "EnableIDPProcessing"})
        self.assertEqual(variables["LEGALDESK_IDP_QUEUE_URL"], {"Ref": "IDPWorkQueueUrl"})
        self.assertEqual(variables["LEGALDESK_IDP_QUEUE_ARN"], {"Ref": "IDPWorkQueueArn"})
        self.assertEqual(variables["LEGALDESK_IDP_RECOVERY_LIMIT_PER_SCOPE"], {"Ref": "IDPRecoveryLimitPerScope"})
        self.assertNotIn("lambda:InvokeFunction", str(dynamo))
        self.assertNotIn("GATEWAY#GRANT", str(dynamo))

    def test_recovery_parameters_have_finite_bounds_and_existing_reconciler_limits_remain(self) -> None:
        params = self.reconciliation["Parameters"]
        self.assertEqual(params["IDPRecoveryLimitPerScope"]["Default"], 25)
        self.assertEqual(params["IDPRecoveryLimitPerScope"]["MinValue"], 1)
        self.assertEqual(params["IDPRecoveryLimitPerScope"]["MaxValue"], 100)
        self.assertEqual(params["LimitPerScope"]["Default"], 25)
        self.assertEqual(params["LimitPerScope"]["MaxValue"], 100)
        self.assertEqual(params["ScheduleExpression"]["Default"], "rate(15 minutes)")
        self.assertNotIn("dynamodb:Scan", str(self.security))
        self.assertNotIn("dynamodb:Scan", str(self.reconciliation))


if __name__ == "__main__":
    unittest.main()
