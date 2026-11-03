"""Discover DLQs from redrive/target contracts, then prove email alarm coverage.

External queue-name parameters bind by <queue logical ID>Name, the convention
used by the operations stack. External alarm-topic parameters represent the
confirmed email topic verified by the deployment runbook; no live AWS calls.
"""
from __future__ import annotations

import copy
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1] / "infra" / "cloudformation"
EMAIL_TOPIC = "arn:aws:sns:eu-west-1:000000000000:confirmed-email-topic"


class Loader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    value = (loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
             else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
             else loader.construct_mapping(node, deep=True))
    return {tag if tag in ("Ref", "Condition") else "Fn::" + tag: value}


Loader.add_multi_constructor("!", intrinsic)


def load_templates():
    return {path.stem: yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)
            for path in ROOT.iterdir() if path.suffix in (".yaml", ".yml")}


class Contracts:
    def __init__(self, templates, managed_topic):
        self.templates = templates
        self.managed_topic = managed_topic
        self.queues = {(stack, name): resource for stack, template in templates.items()
                       for name, resource in template.get("Resources", {}).items()
                       if resource["Type"] == "AWS::SQS::Queue"}

    def parameter(self, stack, name):
        if name == "AWS::NoValue":
            return None
        if name.startswith("AWS::"):
            return {"AWS::Region": "eu-west-1", "AWS::AccountId": "000000000000",
                    "AWS::Partition": "aws", "AWS::StackName": stack}.get(name, name)
        parameter = self.templates[stack].get("Parameters", {}).get(name)
        if parameter is None:
            return f"{stack}/{name}"
        if ":sns:" in parameter.get("AllowedPattern", ""):
            # Only the stack that owns an email topic uses managed-topic mode.
            owns_topic = any(r["Type"] == "AWS::SNS::Subscription" and
                             r.get("Properties", {}).get("Protocol") in ("email", "email-json")
                             for r in self.templates[stack].get("Resources", {}).values())
            return "" if self.managed_topic and owns_topic else EMAIL_TOPIC
        if name == "CreateAlarmTopic":
            return "true" if self.managed_topic else "false"
        bindings = [(s, q) for s, q in self.queues if name == q + "Name"]
        if bindings:
            assert len(bindings) == 1, f"ambiguous external queue binding: {name}"
            return self.attribute(*bindings[0], "QueueName")
        if set(parameter.get("AllowedValues", [])) == {"true", "false"}:
            return "true"  # Check enabled processing, not an empty gated stack.
        return parameter.get("Default", f"{stack}/{name}")

    def attribute(self, stack, name, attribute):
        if (stack, name) in self.queues:
            queue_name = self.value(stack, self.queues[stack, name]["Properties"].get(
                "QueueName", f"{stack}-{name}"))
            if attribute == "QueueName":
                return queue_name
            if attribute == "Arn":
                return f"arn:aws:sqs:eu-west-1:000000000000:{queue_name}"
        return f"{stack}/{name}.{attribute}"

    def condition(self, stack, name):
        return self.value(stack, self.templates[stack]["Conditions"][name])

    def value(self, stack, item):
        if isinstance(item, list):
            return [self.value(stack, v) for v in item]
        if not isinstance(item, dict):
            return item
        if "Ref" in item:
            return self.parameter(stack, item["Ref"])
        if "Condition" in item:
            return self.condition(stack, item["Condition"])
        if "Fn::GetAtt" in item:
            parts = item["Fn::GetAtt"]
            return self.attribute(stack, *(parts.split(".", 1) if isinstance(parts, str) else parts))
        if "Fn::If" in item:
            name, yes, no = item["Fn::If"]
            return self.value(stack, yes if self.condition(stack, name) else no)
        if "Fn::Equals" in item:
            a, b = self.value(stack, item["Fn::Equals"])
            return a == b
        if "Fn::Not" in item:
            return not self.value(stack, item["Fn::Not"][0])
        if "Fn::And" in item:
            return all(self.value(stack, v) for v in item["Fn::And"])
        if "Fn::Or" in item:
            return any(self.value(stack, v) for v in item["Fn::Or"])
        if "Fn::Sub" in item:
            spec = item["Fn::Sub"]
            text, variables = (spec, {}) if isinstance(spec, str) else spec
            def replace(match):
                key = match[1]
                if key in variables:
                    return str(self.value(stack, variables[key]))
                return str(self.attribute(stack, *key.split(".", 1)) if "." in key
                           else self.parameter(stack, key))
            return re.sub(r"\$\{([^}]+)}", replace, text)
        return {k: self.value(stack, v) for k, v in item.items()}

    def active(self, stack, resource):
        return "Condition" not in resource or self.condition(stack, resource["Condition"])

    def verify(self):
        targets = set()
        alarms = []
        email_topics = {EMAIL_TOPIC}
        def discover(stack, node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "deadLetterTargetArn":
                        targets.add(self.value(stack, value))
                    elif key == "DeadLetterConfig":
                        config = self.value(stack, value)
                        targets.add(config.get("Arn", config.get("TargetArn")))
                    else:
                        discover(stack, value)
            elif isinstance(node, list):
                for value in node:
                    discover(stack, value)
        for stack, template in self.templates.items():
            for resource in template.get("Resources", {}).values():
                if not self.active(stack, resource):
                    continue
                discover(stack, resource.get("Properties", {}))
                if resource["Type"] == "AWS::CloudWatch::Alarm":
                    alarms.append(self.value(stack, resource["Properties"]))
                if resource["Type"] == "AWS::SNS::Subscription":
                    props = self.value(stack, resource["Properties"])
                    if props["Protocol"] in ("email", "email-json"):
                        email_topics.add(props["TopicArn"])
        queue_arns = {self.attribute(s, q, "Arn"): (s, q) for s, q in self.queues}
        # Also catch explicitly named, currently unwired DLQs.
        targets.update(self.attribute(s, q, "Arn") for s, q in self.queues
                       if re.search(r"DeadLetter|DLQ", q, re.I))
        assert targets, "no DLQ contracts found"
        for target in targets:
            assert target in queue_arns, f"unresolved DLQ target: {target}"
            stack, queue = queue_arns[target]
            queue_name = self.attribute(stack, queue, "QueueName")
            matches = [a for a in alarms if a.get("Namespace") == "AWS/SQS"
                       and a.get("MetricName") == "ApproximateNumberOfMessagesVisible"
                       and {"Name": "QueueName", "Value": queue_name} in a.get("Dimensions", [])
                       and a.get("Statistic") == "Maximum"
                       and (a.get("ComparisonOperator"), a.get("Threshold")) in {
                           ("GreaterThanThreshold", 0), ("GreaterThanOrEqualToThreshold", 1)}
                       and a.get("EvaluationPeriods") == 1
                       and a.get("DatapointsToAlarm", 1) == 1
                       and 60 <= a.get("Period", 0) <= 300
                       and a.get("ActionsEnabled", True) is True
                       and email_topics.intersection(a.get("AlarmActions") or [])]
            assert matches, f"DLQ without an enabled email alarm: {stack}/{queue}"


class DLQEmailContractTests(unittest.TestCase):
    def test_every_discovered_dlq_has_an_email_alarm_in_both_topic_modes(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                Contracts(load_templates(), managed).verify()

    def test_new_dlq_with_arbitrary_name_cannot_escape_coverage(self):
        templates = load_templates()
        resources = templates["phase-14-idp"]["Resources"]
        resources["UnexpectedQueue"] = {"Type": "AWS::SQS::Queue", "Properties": {}}
        resources["SourceQueue"] = {"Type": "AWS::SQS::Queue", "Properties": {
            "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["UnexpectedQueue", "Arn"]}}}}
        with self.assertRaisesRegex(AssertionError, "UnexpectedQueue"):
            Contracts(templates, False).verify()

    def test_missing_disabled_misdirected_and_non_email_actions_fail(self):
        original = load_templates()
        for stack, template in original.items():
            for name, resource in template.get("Resources", {}).items():
                props = resource.get("Properties", {})
                if resource["Type"] != "AWS::CloudWatch::Alarm" or props.get(
                        "MetricName") != "ApproximateNumberOfMessagesVisible":
                    continue
                for mutation in ({"AlarmActions": []}, {"ActionsEnabled": False},
                                 {"AlarmActions": ["arn:aws:sns:eu-west-1:000000000000:no-email"]},
                                 {"Dimensions": [{"Name": "QueueName", "Value": "wrong-queue"}]},
                                 {"Threshold": 10}):
                    with self.subTest(stack=stack, alarm=name, mutation=mutation):
                        templates = copy.deepcopy(original)
                        templates[stack]["Resources"][name]["Properties"].update(mutation)
                        with self.assertRaisesRegex(AssertionError, "DLQ without"):
                            Contracts(templates, False).verify()


if __name__ == "__main__":
    unittest.main()
