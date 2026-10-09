from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(name: str):
    def constructor(loader: _Loader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {name: value}

    return constructor


for _tag, _name in (
    ("!Ref", "Ref"),
    ("!GetAtt", "Fn::GetAtt"),
    ("!Sub", "Fn::Sub"),
    ("!If", "Fn::If"),
    ("!Equals", "Fn::Equals"),
    ("!Not", "Fn::Not"),
    ("!And", "Fn::And"),
    ("!Select", "Fn::Select"),
    ("!Split", "Fn::Split"),
):
    _Loader.add_constructor(_tag, _intrinsic(_name))


def _load(name: str) -> dict[str, Any]:
    return yaml.load((ROOT / "infra" / "cloudformation" / name).read_text(encoding="utf-8"), Loader=_Loader)


def _statements(template: dict[str, Any], resource: str) -> list[dict[str, Any]]:
    policies = template["Resources"][resource]["Properties"].get("Policies", [])
    values: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict) and "Fn::If" in value:
            branches = value["Fn::If"]
            if isinstance(branches, list) and len(branches) == 3:
                add(branches[1])
            return
        if isinstance(value, dict):
            values.append(value)

    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            add(statement)
    return values


def _sid(template: dict[str, Any], resource: str, sid: str) -> dict[str, Any]:
    matches = [item for item in _statements(template, resource) if item.get("Sid") == sid]
    if len(matches) != 1:
        raise AssertionError(f"expected one {sid}, found {len(matches)}")
    return matches[0]


def _sub_text(value: Any) -> str:
    if isinstance(value, dict) and "Fn::Sub" in value:
        raw = value["Fn::Sub"]
        return raw[0] if isinstance(raw, list) else raw
    return ""


class Phase14IDPM2MIaCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway = _load("phase-08-gateway-mcp.yaml")
        cls.review = _load("phase-07-review-task.yaml")
        cls.idp = _load("phase-14-idp.yaml")
        cls.reconciliation = _load("phase-14-reconciliation.yaml")

    def test_gateway_machine_union_is_opt_in_and_same_tool(self) -> None:
        params = self.gateway["Parameters"]
        self.assertEqual(params["GatewayJwtIDPClientId"]["Default"], "")
        self.assertEqual(params["GatewayJwtIDPScope"]["AllowedValues"], ["legaldesk-idp/review-create"])
        self.assertNotIn("IDPSourceBucketArn", params)
        self.assertIn("HasIDPGatewayConfiguration", self.gateway["Conditions"])
        self.assertIn("HasSecondIDPMatter", self.gateway["Conditions"])
        interceptor_env = self.gateway["Resources"]["GatewayRequestInterceptorFunction"]["Properties"]["Environment"]["Variables"]
        self.assertEqual(interceptor_env["LEGALDESK_IDP_M2M_CLIENT_ID"], {"Ref": "GatewayJwtIDPClientId"})
        self.assertEqual(interceptor_env["LEGALDESK_IDP_REVIEW_SCOPE"], {"Ref": "GatewayJwtIDPScope"})
        self.assertIn("Name: create_review_task", (ROOT / "infra/cloudformation/phase-08-gateway-mcp.yaml").read_text(encoding="utf-8"))
        self.assertIn("invocationId:", (ROOT / "infra/cloudformation/phase-08-gateway-mcp.yaml").read_text(encoding="utf-8"))
        puts = [item for item in _statements(self.gateway, "GatewayRequestInterceptorRole") if item.get("Action") == "dynamodb:PutItem"]
        self.assertEqual(len(puts), 1)
        keys = puts[0]["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
        self.assertIn("GATEWAY#IDP-REVIEW#*", keys)
        s3 = [item for item in _statements(self.gateway, "GatewayRequestInterceptorRole") if item.get("Action") == "s3:GetObject"]
        self.assertEqual(len(s3), 1)
        resources = s3[0]["Resource"]
        self.assertTrue(any(_sub_text(item).endswith("/matters/${MatterId}/documents/*/original.pdf") for item in resources))
        self.assertFalse(any("/matters/*/documents/" in _sub_text(item) for item in resources))

    def test_gateway_update_review_schema_exposes_idp_decision_contract(self) -> None:
        targets = self.gateway["Resources"]["ReviewTaskGatewayTarget"]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        update = next(item for item in targets if item.get("Name") == "update_review_task")
        schema = update["InputSchema"]["Properties"]["idpDecision"]
        self.assertEqual(schema["Type"], "object")
        self.assertEqual(schema["Required"], ["fieldName", "action", "reason", "evidence"])
        self.assertEqual(schema["Properties"]["action"]["Type"], "string")
        self.assertIn("APPROVE", schema["Properties"]["action"]["Description"])
        self.assertIn("proposedValueJson", schema["Properties"])
        self.assertEqual(schema["Properties"]["proposedValueJson"]["Type"], "string")
        evidence = schema["Properties"]["evidence"]
        self.assertEqual(evidence["Type"], "array")
        self.assertEqual(evidence["Items"]["Required"], ["page", "quote", "contentSha256"])
        self.assertEqual(set(evidence["Items"]["Properties"]), {"page", "quote", "contentSha256", "start", "end"})
        self.assertNotIn("origin", schema["Properties"])
        self.assertNotIn("presence", schema["Properties"])
        self.assertNotIn("acceptance", schema["Properties"])

        allowed_types = {"string", "number", "object", "array", "boolean", "integer"}
        allowed_schema_keys = {"Type", "Properties", "Required", "Items", "Description"}

        def validate(node: Any, path: str) -> None:
            self.assertIsInstance(node, dict, path)
            self.assertTrue(set(node).issubset(allowed_schema_keys), path)
            self.assertIn(node.get("Type"), allowed_types, path)
            self.assertNotIn("AllowedValues", node, path)
            if node["Type"] == "object":
                for name, child in node.get("Properties", {}).items():
                    validate(child, f"{path}.{name}")
            elif node["Type"] == "array":
                validate(node["Items"], f"{path}[]")

        validate(schema, "update_review_task.idpDecision")

    def test_review_target_hash_read_is_opt_in_and_canonical_only(self) -> None:
        params = self.review["Parameters"]
        self.assertEqual(params["IDPM2MClientId"]["Default"], "")
        self.assertNotIn("IDPSourceBucketArn", params)
        statement = next(item for item in _statements(self.review, "ReviewTaskExecutionRole") if item.get("Sid") == "ReadCanonicalIDPDocuments")
        self.assertEqual(statement["Action"], "s3:GetObject")
        sub_values = []
        for value in statement["Resource"]:
            if isinstance(value, dict) and "Fn::If" in value:
                value = value["Fn::If"][1]
            rendered = _sub_text(value)
            if rendered:
                sub_values.append(rendered)
        self.assertTrue(any("/tenants/${TenantId}/matters/${MatterId}/documents/*/original.pdf" in value for value in sub_values))
        self.assertTrue(any("/tenants/${TenantId}/matters/${MatterId}/documents/*/original.txt" in value for value in sub_values))
        self.assertTrue(any("/idp-artifacts/tenant=${TenantId}/matter=${MatterId}/document=*/run=*/pages-*" in value for value in sub_values))
        self.assertFalse(any("/matters/*/documents/" in value for value in sub_values))
        self.assertNotIn("s3:DeleteObject", str(statement))
        env = self.review["Resources"]["ReviewTaskFunction"]["Properties"]["Environment"]["Variables"]
        self.assertEqual(env["LEGALDESK_IDP_REVIEW_SCOPE"], {"Ref": "IDPReviewScope"})
        self.assertEqual(env["LEGALDESK_IDP_REVIEW_TENANT_ID"], {"Ref": "IDPReviewTenantId"})
        self.assertEqual(env["LEGALDESK_IDP_REVIEW_MATTER_IDS"], {"Ref": "IDPReviewMatterIds"})

    def test_idp_roles_write_only_invocation_namespace_when_review_gate_is_on(self) -> None:
        self.assertEqual(self.idp["Parameters"]["EnableIDPReviewDispatch"]["Default"], "false")
        for role in ("IDPWorkerExecutionRole", "IDPOCRContinuationExecutionRole"):
            statements = _statements(self.idp, role)
            candidates = [item for item in statements if item.get("Sid") == "PersistIDPReviewInvocationsOnly"]
            self.assertEqual(len(candidates), 1)
            policy = candidates[0]
            self.assertEqual(set(policy["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"})
            self.assertEqual(policy["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"], ["GATEWAY#IDP-INVOCATION#*"])
        self.assertNotIn("GATEWAY#IDP-REVIEW#*", str(self.idp))
        for function in ("IDPWorkerFunction", "IDPOCRContinuationFunction"):
            env = self.idp["Resources"][function]["Properties"]["Environment"]["Variables"]
            self.assertEqual(env["LEGALDESK_IDP_GATEWAY_URL"], {"Ref": "ExistingGatewayUrl"})
            self.assertEqual(env["LEGALDESK_IDP_REVIEW_ENABLED"], {"Ref": "EnableIDPReviewDispatch"})

    def test_reconciliation_review_recovery_is_bounded_and_secret_scoped(self) -> None:
        self.assertEqual(self.reconciliation["Parameters"]["EnableIDPReviewDispatch"]["Default"], "false")
        statement = _sid(self.reconciliation, "ReconciliationExecutionRole", "PersistIDPReviewInvocationsOnly")
        self.assertEqual(set(statement["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"})
        self.assertEqual(statement["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"], ["GATEWAY#IDP-INVOCATION#*"])
        secret = _sid(self.reconciliation, "ReconciliationExecutionRole", "ReadOperatorBootstrappedM2MSecretParameter")
        self.assertEqual(secret["Action"], "ssm:GetParameter")
        self.assertEqual(secret["Resource"], {"Ref": "M2MSecretParameterArn"})
        env = self.reconciliation["Resources"]["ReconciliationFunction"]["Properties"]["Environment"]["Variables"]
        for name in ("LEGALDESK_IDP_REVIEW_ENABLED", "LEGALDESK_IDP_GATEWAY_URL", "LEGALDESK_IDP_TOKEN_ENDPOINT", "LEGALDESK_IDP_M2M_CLIENT_ID", "LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME", "LEGALDESK_IDP_REVIEW_SCOPE"):
            self.assertIn(name, env)
        self.assertNotIn("dynamodb:Scan", str(self.reconciliation))


if __name__ == "__main__":
    unittest.main()
