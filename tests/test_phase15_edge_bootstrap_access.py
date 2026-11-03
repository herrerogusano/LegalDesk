"""Who can use the temporary Public Edge role, and which of its actions are sensitive (PB-002 of PR #68).

The role can write inline policies in two roles. The protection is not a boundary on the role but the facts pinned here:
only a principal with iam:PassRole on it can hand it to CloudFormation, the CD roles cannot, and the sensitive actions exist
only in the stage that needs them, only on the intended ARNs.
"""

from __future__ import annotations

import fnmatch
import re
import unittest
from pathlib import Path
from typing import Any

from tests.test_phase15_edge_bootstrap_iac import (
    ACCOUNT, BOOTSTRAP, PARAMETERS, REGION, STACK, actions, generated_name, load, resources, statements,
)

ROOT = Path(__file__).parents[1]
CD_IAM = ROOT / "infra" / "cloudformation" / "cd-iam.yaml"
# The role CloudFormation creates for the bootstrap stack; generated, so truncated to 64 characters.
TEMP_ROLE_NAME = generated_name("LegalDeskPhase15EdgeBootstrap", "EdgeAgentChatCloudFormationRole", "ABCDEF123456")
TEMP_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{TEMP_ROLE_NAME}"
CODE_ONLY_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/LegalDeskProductionCloudFormation"
WORKER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/LegalDeskPhase14PublicEdge-AgentChatWorker*"
APPLICATION_ROLE = PARAMETERS["ExistingApplicationRoleArn"]

SENSITIVE = {
    "iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:PassRole",
}


def cd_statements(role: str) -> list[dict[str, Any]]:
    template = load(CD_IAM)
    return template["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def is_code_only_arn(value: Any) -> bool:
    """``!GetAtt CloudFormationExecutionRole.Arn`` in either of its two YAML spellings."""

    return isinstance(value, dict) and value in ({"Fn::GetAtt": "CloudFormationExecutionRole.Arn"}, {"Fn::GetAtt": ["CloudFormationExecutionRole", "Arn"]})


def render_cd_resource(value: Any) -> str:
    """The ARN a CD statement resolves to, with sample parameter values (the code-only role for its own Arn)."""

    if is_code_only_arn(value):
        return CODE_ONLY_ROLE_ARN
    if isinstance(value, dict) and set(value) == {"Ref"}:
        return {
            "ApplicationExecutionRoleArn": APPLICATION_ROLE,
            "AgentChatWorkerExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{generated_name(STACK, 'AgentChatWorkerExecutionRole', 'ABCDEF123456')}",
        }.get(value["Ref"], f"<{value['Ref']}>")
    return str(value)


class WhoCanPassTheTemporaryRoleTests(unittest.TestCase):
    def test_the_temporary_role_name_model_is_what_the_other_tests_assume(self) -> None:
        self.assertEqual(len(TEMP_ROLE_NAME), 64)
        self.assertTrue(TEMP_ROLE_NAME.startswith("LegalDeskPhase15EdgeBootstrap-"))

    def test_the_release_role_of_the_cd_can_pass_only_the_code_only_role_and_only_to_cloudformation(self) -> None:
        passes = [s for s in cd_statements("ProductionDeploymentRole") if "iam:PassRole" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertEqual(len(passes), 1)
        statement = passes[0]
        self.assertEqual([render_cd_resource(statement["Resource"])], [CODE_ONLY_ROLE_ARN])
        self.assertEqual(statement["Condition"], {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}})
        self.assertFalse(fnmatch.fnmatchcase(TEMP_ROLE_ARN, render_cd_resource(statement["Resource"])))

    def test_the_release_role_cannot_choose_another_role_for_its_change_sets(self) -> None:
        creates = [s for s in cd_statements("ProductionDeploymentRole") if "cloudformation:CreateChangeSet" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertEqual(len(creates), 1)
        condition = creates[0]["Condition"]
        self.assertEqual(set(condition), {"StringEquals"})
        self.assertEqual(set(condition["StringEquals"]), {"cloudformation:RoleArn"})
        self.assertTrue(is_code_only_arn(condition["StringEquals"]["cloudformation:RoleArn"]))

    def test_the_release_role_has_no_way_to_create_update_or_delete_stacks_or_to_reach_the_bootstrap_stack(self) -> None:
        template = load(CD_IAM)
        for statement in cd_statements("ProductionDeploymentRole"):
            acts = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            for action in acts:
                self.assertNotIn(action, {"cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack", "cloudformation:SetStackPolicy"})
            if any(a.startswith("cloudformation:") and a != "cloudformation:ValidateTemplate" for a in acts):
                self.assertEqual(statement["Resource"], {"Ref": "PublicStackArn"})
        pattern = template["Parameters"]["PublicStackArn"]["AllowedPattern"]
        bootstrap_stack = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/LegalDeskPhase15EdgeBootstrap/abcd1234-0000-0000-0000-000000000000"
        public_stack = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/LegalDeskPhase14PublicEdge/abcd1234-0000-0000-0000-000000000000"
        self.assertIsNone(re.fullmatch(pattern, bootstrap_stack))
        self.assertIsNotNone(re.fullmatch(pattern, public_stack))

    def test_the_code_only_cloudformation_role_can_pass_only_the_application_and_worker_roles_to_lambda(self) -> None:
        passes = [s for s in cd_statements("CloudFormationExecutionRole") if "iam:PassRole" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertEqual(len(passes), 2)
        for statement in passes:
            self.assertEqual(statement["Condition"], {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}})
            self.assertFalse(fnmatch.fnmatchcase(TEMP_ROLE_ARN, render_cd_resource(statement["Resource"])))

    def test_no_value_the_cd_role_parameters_accept_can_be_the_temporary_role(self) -> None:
        # ApplicationExecutionRoleArn and AgentChatWorkerExecutionRoleArn feed PassRole/read statements: they may only name this stack's own roles.
        parameters = load(CD_IAM)["Parameters"]
        for name in ("ApplicationExecutionRoleArn", "AgentChatWorkerExecutionRoleArn"):
            self.assertIsNone(re.fullmatch(parameters[name]["AllowedPattern"], TEMP_ROLE_ARN), name)
        self.assertIsNotNone(re.fullmatch(parameters["ApplicationExecutionRoleArn"]["AllowedPattern"], APPLICATION_ROLE))
        self.assertIsNotNone(re.fullmatch(parameters["AgentChatWorkerExecutionRoleArn"]["AllowedPattern"], f"arn:aws:iam::{ACCOUNT}:role/{generated_name(STACK, 'AgentChatWorkerExecutionRole', 'ABCDEF123456')}"))

    def test_the_temporary_role_can_be_assumed_only_by_cloudformation_and_cannot_pass_itself(self) -> None:
        trust = load(BOOTSTRAP)["Resources"]["EdgeAgentChatCloudFormationRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual([(s["Principal"], s["Action"]) for s in trust], [({"Service": "cloudformation.amazonaws.com"}, "sts:AssumeRole")])
        for stage in ("code-and-quota", "agent-chat"):
            for statement in statements(stage):
                if "iam:PassRole" in actions(statement):
                    for resource in resources(statement):
                        self.assertFalse(fnmatch.fnmatchcase(TEMP_ROLE_ARN, resource), resource)
                self.assertNotIn("sts:AssumeRole", actions(statement))

    def test_the_cd_workflow_always_passes_the_code_only_role_explicitly(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "production-cd.yml").read_text(encoding="utf-8")
        self.assertIn('--execution-role-arn "$AWS_CLOUDFORMATION_ROLE_ARN"', workflow)


class SensitiveActionsTests(unittest.TestCase):
    """CreateRole / PutRolePolicy and friends: only in the stage agent-chat, only on the intended ARNs, never in code-and-quota."""

    def sensitive(self, stage: str) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for statement in statements(stage):
            for action in actions(statement):
                if action in SENSITIVE or action.startswith("iam:") and action not in {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"}:
                    found.setdefault(action, set()).update(resources(statement))
        return found

    def test_stage_one_has_no_sensitive_action_but_passing_the_application_role(self) -> None:
        self.assertEqual(self.sensitive("code-and-quota"), {"iam:PassRole": {APPLICATION_ROLE}})

    def test_stage_two_has_exactly_these_sensitive_actions_on_exactly_these_resources(self) -> None:
        found = self.sensitive("agent-chat")
        self.assertEqual(found.pop("iam:PutRolePolicy"), {WORKER_ROLE, APPLICATION_ROLE})
        self.assertEqual(found.pop("iam:PassRole"), {APPLICATION_ROLE, WORKER_ROLE})
        for action in ("iam:CreateRole", "iam:DeleteRole", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"):
            self.assertEqual(found.pop(action), {WORKER_ROLE}, action)
        self.assertEqual(found, {}, "any other IAM write action is a new sensitive action: review it")

    def test_the_application_role_can_only_have_its_inline_policy_replaced(self) -> None:
        on_application_role = {a for s in statements("agent-chat") if APPLICATION_ROLE in resources(s) for a in actions(s)}
        self.assertEqual(
            on_application_role,
            {"iam:PassRole", "iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy"} | {a for a in on_application_role if not a.startswith("iam:")},
        )
        self.assertNotIn("iam:DeleteRolePolicy", on_application_role)  # cannot remove the policy, only replace it by name
        self.assertNotIn("iam:DeleteRole", on_application_role)
        for forbidden in ("iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary", "iam:CreatePolicy"):
            self.assertNotIn(forbidden, {a for stage in ("code-and-quota", "agent-chat") for s in statements(stage) for a in actions(s)})

    def test_sensitive_resources_are_never_wildcards_beyond_the_one_worker_prefix(self) -> None:
        for stage in ("code-and-quota", "agent-chat"):
            for statement in statements(stage):
                if set(actions(statement)) & SENSITIVE:
                    for resource in resources(statement):
                        self.assertIn(resource, {APPLICATION_ROLE, WORKER_ROLE}, f"{stage}: {statement['Sid']}")
                        if "*" in resource:
                            self.assertEqual(resource, WORKER_ROLE)

    def test_the_documents_state_that_inline_policies_can_be_written_in_two_roles(self) -> None:
        runbook = (ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8")
        step_8 = " ".join(runbook[runbook.index("### 8.0 "):runbook.index("### 8a ")].split())  # line breaks are not content
        self.assertNotIn("It cannot attach policies", step_8)
        for needle in ("write inline policies", "exactly two places", "Who can use it", "LegalDeskProductionDeployment", "Why no permissions boundary", "Why the window is short", "test_phase15_edge_bootstrap_access"):
            self.assertIn(needle, step_8)
        template = " ".join(BOOTSTRAP.read_text(encoding="utf-8").split())
        self.assertIn("inline policies in two roles only", template)


if __name__ == "__main__":
    unittest.main()
