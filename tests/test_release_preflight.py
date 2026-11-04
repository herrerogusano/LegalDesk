import copy
import unittest
from unittest.mock import Mock
from scripts import release_preflight as check
from scripts import phase15_changeset_preview as preview


class PreflightTests(unittest.TestCase):
    def test_description_drift_and_parameter_names_are_visible(self):
        local = {"Description": "a", "Parameters": {"P": {"Description": "a"}},
                 "Resources": {"R": {"Properties": {"Description": "a"}}}}
        deployed = copy.deepcopy(local)
        self.assertTrue(check.compare(local, deployed)["compatible"])
        deployed["Parameters"]["P"]["Description"] = "b"
        deployed["Resources"]["R"]["Properties"]["Description"] = "b"
        deployed["Description"] = "b"
        result = check.compare(local, deployed)
        self.assertFalse(result["compatible"])
        self.assertEqual(result["differences"], {"Parameters": ["P"], "Resources": ["R"], "otherSections": ["Description"]})

    def test_original_template_only_no_writes_for_string_and_mapping(self):
        path = check.ROOT / "infra/cloudformation/phase-14-public-edge.yaml"
        for body in (path.read_text(encoding="utf-8"), check._load_yaml(path)):
            client = Mock(spec=["get_template"])
            client.get_template.return_value = {"TemplateBody": body}
            self.assertTrue(check.preflight(client, path)["compatible"])
            client.get_template.assert_called_once_with(StackName=check.STACK_NAME, TemplateStage="Original")

    def test_iteration_profile_rejects_replacement_and_unrelated_changes(self):
        profile = preview.load_profile("harness-max-iterations")
        change = {"StackName": "legaldesk-phase-01", "Changes": [{"ResourceChange": {"Action": "Modify", "LogicalResourceId": "MinimalHarness",
                  "ResourceType": "AWS::BedrockAgentCore::Harness", "Replacement": "False",
                  "Details": [{"Target": {"Attribute": "Properties", "Name": "MaxIterations"}}]}}]}
        self.assertEqual(preview.check_change_set(change, profile), [])
        for replacement in ("True", "Conditional", None):
            change["Changes"][0]["ResourceChange"]["Replacement"] = replacement
            self.assertTrue(preview.check_change_set(change, profile))
        change["Changes"] = []
        self.assertTrue(preview.check_change_set(change, profile))
