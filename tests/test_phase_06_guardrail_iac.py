from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-06-guardrails.yaml"


class Phase06GuardrailInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_template_contains_only_guardrail_and_immutable_version_resources(self) -> None:
        resource_types = re.findall(
            r"^  [A-Za-z][A-Za-z0-9]*:\n    Type: (AWS::[A-Za-z0-9:]+)",
            self.template,
            re.MULTILINE,
        )
        self.assertEqual(
            resource_types,
            ["AWS::Bedrock::Guardrail", "AWS::Bedrock::GuardrailVersion"],
        )
        self.assertNotIn("AWS::IAM::", self.template)
        self.assertNotIn("Action: \"*\"", self.template)

    def test_prompt_attack_is_input_only_and_harmful_categories_block_both(self) -> None:
        content_policy = re.search(
            r"(?ms)^      ContentPolicyConfig:\n(?P<body>.*?)(?=^      SensitiveInformationPolicyConfig:)",
            self.template,
        )
        self.assertIsNotNone(content_policy)
        content_body = content_policy.group("body")
        configured_filters = re.findall(
            r"(?m)^          - Type: ([A-Z_]+)$", content_body
        )
        self.assertEqual(
            configured_filters,
            ["PROMPT_ATTACK", "HATE", "INSULTS", "VIOLENCE", "SEXUAL", "MISCONDUCT"],
        )
        for filter_type in configured_filters:
            block = re.search(
                rf"(?ms)^          - Type: {filter_type}\n(?P<body>.*?)(?=^          - Type:|\Z)",
                content_body,
            )
            self.assertIsNotNone(block, filter_type)
            self.assertIn("InputStrength: HIGH", block.group("body"))
            self.assertIn("InputAction: BLOCK", block.group("body"))
            self.assertIn("InputEnabled: true", block.group("body"))
            if filter_type == "PROMPT_ATTACK":
                self.assertIn("OutputStrength: NONE", block.group("body"))
                self.assertIn("OutputEnabled: false", block.group("body"))
                self.assertIn("OutputAction: NONE", block.group("body"))
            else:
                self.assertIn("OutputStrength: HIGH", block.group("body"))
                self.assertIn("OutputAction: BLOCK", block.group("body"))
                self.assertIn("OutputEnabled: true", block.group("body"))

    def test_pii_policy_masks_contact_identity_and_blocks_high_risk_secrets(self) -> None:
        pii_section = re.search(
            r"(?ms)^      SensitiveInformationPolicyConfig:\n(?P<body>.*?)(?=^      TopicPolicyConfig:)",
            self.template,
        )
        self.assertIsNotNone(pii_section)
        body = pii_section.group("body")
        for entity_type in ("NAME", "EMAIL", "PHONE", "ADDRESS"):
            entity = re.search(
                rf"(?ms)^          - Type: {entity_type}\n(?P<body>.*?)(?=^          - Type:|\Z)",
                body,
            )
            self.assertIsNotNone(entity, entity_type)
            self.assertIn("Action: ANONYMIZE", entity.group("body"))
            self.assertIn("InputEnabled: true", entity.group("body"))
            self.assertIn("InputAction: ANONYMIZE", entity.group("body"))
            self.assertIn("OutputEnabled: true", entity.group("body"))
            self.assertIn("OutputAction: ANONYMIZE", entity.group("body"))
        for entity_type in (
            "US_SOCIAL_SECURITY_NUMBER",
            "CREDIT_DEBIT_CARD_NUMBER",
            "AWS_ACCESS_KEY",
            "AWS_SECRET_KEY",
            "PASSWORD",
        ):
            entity = re.search(
                rf"(?ms)^          - Type: {entity_type}\n(?P<body>.*?)(?=^          - Type:|\Z)",
                body,
            )
            self.assertIsNotNone(entity, entity_type)
            self.assertIn("Action: BLOCK", entity.group("body"))
            self.assertIn("InputEnabled: true", entity.group("body"))
            self.assertIn("InputAction: BLOCK", entity.group("body"))
            self.assertIn("OutputEnabled: true", entity.group("body"))
            self.assertIn("OutputAction: BLOCK", entity.group("body"))

    def test_denied_topic_is_limited_to_individualized_legal_advice(self) -> None:
        topic = re.search(
            r"(?ms)^          - Name: IndividualizedLegalAdvice\n(?P<body>.*?)(?=^      ContextualGroundingPolicyConfig:)",
            self.template,
        )
        self.assertIsNotNone(topic)
        body = topic.group("body")
        self.assertIn("Type: DENY", body)
        self.assertIn("InputAction: BLOCK", body)
        self.assertIn("OutputAction: BLOCK", body)
        self.assertIn("general legal information", body)
        self.assertIn("specific", body)

    def test_contextual_grounding_thresholds_and_block_actions_are_explicit(self) -> None:
        grounding = re.search(
            r"(?ms)^      ContextualGroundingPolicyConfig:\n(?P<body>.*?)(?=^      Tags:)",
            self.template,
        )
        self.assertIsNotNone(grounding)
        body = grounding.group("body")
        self.assertRegex(
            body,
            r"(?s)- Type: GROUNDING\n\s+Threshold: 0\.75\n\s+Enabled: true\n\s+Action: BLOCK",
        )
        self.assertRegex(
            body,
            r"(?s)- Type: RELEVANCE\n\s+Threshold: 0\.5\n\s+Enabled: true\n\s+Action: BLOCK",
        )

    def test_new_version_description_is_a_changeable_release_marker(self) -> None:
        self.assertIn("GuardrailVersionDescription:", self.template)
        self.assertIn("Description: !Ref GuardrailVersionDescription", self.template)
        self.assertIn("GuardrailIdentifier: !GetAtt LegalDeskGuardrail.GuardrailId", self.template)
        self.assertIn("Value: !GetAtt LegalDeskGuardrailVersion.Version", self.template)


if __name__ == "__main__":
    unittest.main()
