"""Offline change-set preview and gate for the PLAN_15 T9 rollout.

Read-only commands, none of which imports boto3 or touches the network (``params``, ``cd-template-match``
and the two below):

``preview``
    Compares the *deployed* template (a backup saved with
    ``aws cloudformation get-template`` or, for a rehearsal, ``--deployed-git-ref``) with
    the template in the repository, after resolving the parameter values given for each
    side (``Ref`` to a parameter, ``Fn::If`` over conditions).  It prints which resources
    would be added, modified or removed, which properties change, the replacement risk and
    every IAM statement that is added, removed or changed.  It is an approximation of what
    CloudFormation will do: the ``Replacement`` field of the real change set is
    authoritative, which is what ``check-change-set`` enforces.

``check-change-set``
    Reads the JSON of a real ``aws cloudformation describe-change-set`` (saved to a file by
    the operator) and fails unless it is exactly what the named profile in
    ``phase15_t9_expectations.json`` allows: only the listed additions, only the listed
    modifications and properties, no removal, no ``Replacement`` other than ``False``.

Exit code 0 = within the profile, 1 = violations, 2 = unusable input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

EXPECTATIONS_PATH = Path(__file__).with_name("phase15_t9_expectations.json")

NO_VALUE = object()
UNKNOWN = object()

IAM_TYPES = {"AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::User", "AWS::IAM::Group"}

# Properties whose change makes CloudFormation replace the resource (heuristic; the real
# change set decides).  A type missing here is reported as UNKNOWN, never as safe.
REPLACEMENT_PROPERTIES: dict[str, frozenset[str]] = {
    "AWS::SQS::Queue": frozenset({"QueueName", "FifoQueue"}),
    "AWS::IAM::Role": frozenset({"RoleName", "Path"}),
    "AWS::Logs::LogGroup": frozenset({"LogGroupName"}),
    "AWS::Logs::MetricFilter": frozenset({"LogGroupName", "FilterName"}),
    "AWS::Lambda::Function": frozenset({"FunctionName"}),
    "AWS::Lambda::EventSourceMapping": frozenset({"EventSourceArn", "SelfManagedEventSource"}),
    "AWS::CloudWatch::Alarm": frozenset({"AlarmName"}),
    "AWS::BedrockAgentCore::GatewayTarget": frozenset({"GatewayIdentifier", "Name"}),
}


class PreviewError(RuntimeError):
    """Unusable input (template, parameter or profile)."""


# --------------------------------------------------------------------------- templates


def load_template(text: str) -> Mapping[str, Any]:
    """Parse a CloudFormation template (YAML with short-form intrinsics, or JSON)."""

    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader: Any, suffix: str, node: Any) -> Any:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        if suffix == "Ref":
            return {"Ref": value}
        if suffix == "Condition":
            return {"Condition": value}
        if suffix == "GetAtt" and isinstance(value, str):
            value = value.split(".", 1)
        return {"Fn::" + suffix: value}

    Loader.add_multi_constructor("!", intrinsic)
    try:
        data = yaml.load(text, Loader=Loader)
    except yaml.YAMLError as exc:
        raise PreviewError("template_invalid") from exc
    if isinstance(data, Mapping) and isinstance(data.get("TemplateBody"), Mapping):
        data = data["TemplateBody"]  # the whole `aws cloudformation get-template` response
    if not isinstance(data, Mapping) or not isinstance(data.get("Resources"), Mapping):
        raise PreviewError("template_invalid")
    return data


def _parse_params(items: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise PreviewError("parameter_invalid")
        result[name] = value
    return result


class Context:
    """Parameter values and condition results for one side of the comparison."""

    def __init__(self, template: Mapping[str, Any], overrides: Mapping[str, str]) -> None:
        self.params: dict[str, Any] = {}
        for name, spec in (template.get("Parameters") or {}).items():
            if name in overrides:
                self.params[name] = str(overrides[name])
            elif isinstance(spec, Mapping) and "Default" in spec and not spec.get("NoEcho"):
                self.params[name] = str(spec["Default"])
        self.conditions: Mapping[str, Any] = template.get("Conditions") or {}
        self._cache: dict[str, Any] = {}

    def condition(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = UNKNOWN  # also breaks any (invalid) cycle
            self._cache[name] = self._evaluate(self.conditions.get(name))
        return self._cache[name]

    def _evaluate(self, node: Any) -> Any:
        if not isinstance(node, Mapping) or len(node) != 1:
            return UNKNOWN
        (key, value), = node.items()
        if key == "Condition":
            return self.condition(value)
        if key == "Fn::Equals":
            left, right = (resolve(item, self) for item in value)
            if isinstance(left, (str, int, float)) and isinstance(right, (str, int, float)):
                return str(left) == str(right)
            return UNKNOWN
        if key == "Fn::Not":
            inner = self._evaluate(value[0])
            return UNKNOWN if inner is UNKNOWN else not inner
        if key in {"Fn::And", "Fn::Or"}:
            parts = [self._evaluate(item) for item in value]
            if key == "Fn::And":
                if any(part is False for part in parts):
                    return False
                return True if all(part is True for part in parts) else UNKNOWN
            if any(part is True for part in parts):
                return True
            return False if all(part is False for part in parts) else UNKNOWN
        return UNKNOWN


def resolve(node: Any, context: Context) -> Any:
    """Substitute known parameters and choose ``Fn::If`` branches; leave the rest symbolic."""

    if isinstance(node, Mapping):
        if len(node) == 1:
            (key, value), = node.items()
            if key == "Ref":
                if value == "AWS::NoValue":
                    return NO_VALUE
                known = context.params.get(value, UNKNOWN)
                return node if known is UNKNOWN else known
            if key == "Fn::If" and isinstance(value, list) and len(value) == 3:
                chosen = context.condition(value[0])
                if chosen is not UNKNOWN:
                    return resolve(value[1] if chosen else value[2], context)
        out: dict[str, Any] = {}
        for key, value in node.items():
            resolved = resolve(value, context)
            if resolved is not NO_VALUE:
                out[key] = resolved
        return out
    if isinstance(node, list):
        return [item for item in (resolve(item, context) for item in node) if item is not NO_VALUE]
    return node


def effective_resources(template: Mapping[str, Any], context: Context) -> dict[str, dict[str, Any]]:
    """Resources that exist for these parameter values, with resolved properties."""

    result: dict[str, dict[str, Any]] = {}
    for logical_id, spec in template["Resources"].items():
        condition = spec.get("Condition") if isinstance(spec, Mapping) else None
        present = True if condition is None else context.condition(condition)
        if present is False:
            continue
        properties = resolve(spec.get("Properties") or {}, context)
        result[logical_id] = {
            "type": spec.get("Type"),
            "properties": properties if isinstance(properties, dict) else {},
            "conditionUnknown": present is UNKNOWN,
        }
    return result


# ------------------------------------------------------------------------------ diff


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _statement_label(statement: Any, position: int) -> str:
    if isinstance(statement, Mapping) and isinstance(statement.get("Sid"), str):
        return statement["Sid"]
    return "anon:" + hashlib.sha256(_canon(statement).encode()).hexdigest()[:8]


def _iam_statements(resource: Mapping[str, Any]) -> dict[tuple[str, str], Any]:
    """(policy, statement label) -> statement, for the policies of an IAM resource."""

    properties = resource["properties"]
    found: dict[tuple[str, str], Any] = {}
    for policy in properties.get("Policies") or []:
        if not isinstance(policy, Mapping):
            continue
        name = str(policy.get("PolicyName", "?"))
        document = policy.get("PolicyDocument")
        statements = document.get("Statement") if isinstance(document, Mapping) else None
        for position, statement in enumerate(statements if isinstance(statements, list) else []):
            found[(name, _statement_label(statement, position))] = statement
    document = properties.get("PolicyDocument")  # AWS::IAM::Policy / ManagedPolicy
    if isinstance(document, Mapping) and isinstance(document.get("Statement"), list):
        for position, statement in enumerate(document["Statement"]):
            found[("PolicyDocument", _statement_label(statement, position))] = statement
    for name in ("AssumeRolePolicyDocument", "ManagedPolicyArns", "PermissionsBoundary"):
        if name in properties:
            found[(name, "-")] = properties[name]
    return found


def _strings(node: Any) -> list[str]:
    """Every string inside a policy value, including the parts of ``Fn::Sub`` / ``Fn::Join``."""

    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def wide_reasons(statement: Any) -> list[str]:
    """Why a statement widens access; empty when it is exact.

    ``NotAction`` / ``NotResource`` (everything except...), a ``*`` action or an action wildcard, a bare ``*``
    resource, and any partial resource wildcard (``arn:...:*``, ``bucket/*``, a path with ``*``) are all
    expansions that the expectation profile must allow explicitly (``wideAllowed``).
    """

    if not isinstance(statement, Mapping):
        return []
    reasons: list[str] = []
    for key in ("NotAction", "NotResource", "NotPrincipal"):
        if key in statement:
            reasons.append(key)
    if any("*" in text or "?" in text for text in _strings(statement.get("Action"))):
        reasons.append("action_wildcard")
    top = statement.get("Resource")
    # Bare "*" only counts as the whole resource; a "*" piece of an Fn::Join/Fn::Sub builds a partial wildcard.
    resources = _strings(top)
    if "*" in (top if isinstance(top, list) else [top]):
        reasons.append("resource_wildcard")
    elif any("*" in text or "?" in text for text in resources):
        reasons.append("resource_partial_wildcard")
    return reasons


def _iam_changes(logical_id: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    old = _iam_statements(before) if before else {}
    new = _iam_statements(after) if after else {}
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            kind = "added"
        elif key not in new:
            kind = "removed"
        elif _canon(old[key]) != _canon(new[key]):
            kind = "changed"
        else:
            continue
        reasons = [] if kind == "removed" or key[0] in {"AssumeRolePolicyDocument", "ManagedPolicyArns", "PermissionsBoundary"} else wide_reasons(new.get(key))
        changes.append({"resource": logical_id, "policy": key[0], "sid": key[1], "change": kind, "wide": bool(reasons), "wideReasons": reasons})
    return changes


def _replacement(resource_type: str, properties: Sequence[str]) -> str:
    immutable = REPLACEMENT_PROPERTIES.get(resource_type)
    if immutable is None:
        return "UNKNOWN"
    return "REPLACEMENT" if immutable.intersection(properties) else "no"


def compare(deployed: Mapping[str, Any], local: Mapping[str, Any], deployed_params: Mapping[str, str], local_params: Mapping[str, str]) -> dict[str, Any]:
    """The preview: added / modified / removed resources, replacement risk and IAM changes."""

    old = effective_resources(deployed, Context(deployed, deployed_params))
    new = effective_resources(local, Context(local, local_params))
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified: dict[str, dict[str, Any]] = {}
    for logical_id in sorted(set(old) & set(new)):
        before, after = old[logical_id], new[logical_id]
        if before["type"] != after["type"]:
            modified[logical_id] = {"type": after["type"], "properties": ["<Type>"], "replacement": "REPLACEMENT"}
            continue
        names = sorted(
            name for name in set(before["properties"]) | set(after["properties"])
            if _canon(before["properties"].get(name)) != _canon(after["properties"].get(name))
        )
        if names:
            modified[logical_id] = {"type": after["type"], "properties": names, "replacement": _replacement(after["type"], names)}
    iam: list[dict[str, Any]] = []
    for logical_id in sorted(set(old) | set(new)):
        resource = new.get(logical_id) or old.get(logical_id)
        if resource and resource["type"] in IAM_TYPES:
            iam.extend(_iam_changes(logical_id, old.get(logical_id), new.get(logical_id)))
    return {
        "added": [{"id": i, "type": new[i]["type"]} for i in added],
        "removed": [{"id": i, "type": old[i]["type"]} for i in removed],
        "modified": [{"id": i, **modified[i]} for i in modified],
        "iam": iam,
        "conditionUnknown": sorted(i for i, r in new.items() if r["conditionUnknown"]),
        "newParameters": sorted(set(local.get("Parameters") or {}) - set(deployed.get("Parameters") or {})),
        "removedParameters": sorted(set(deployed.get("Parameters") or {}) - set(local.get("Parameters") or {})),
    }


# -------------------------------------------------------------------------- profiles


def load_profile(name: str, path: Path = EXPECTATIONS_PATH) -> Mapping[str, Any]:
    try:
        profiles = json.loads(path.read_text(encoding="utf-8"))["profiles"]
        profile = profiles[name]
    except (OSError, ValueError, KeyError) as exc:
        raise PreviewError("profile_unknown") from exc
    if not isinstance(profile, Mapping):
        raise PreviewError("profile_unknown")
    return profile


def _violations_for(profile: Mapping[str, Any], added: Mapping[str, str], modified: Mapping[str, Sequence[str]], removed: Sequence[str], replacements: Mapping[str, str], iam_resources: Sequence[str]) -> list[str]:
    """Shared rules; ``added`` / ``modified`` are logical id -> type / properties."""

    problems: list[str] = []
    expected_add = set(profile.get("add", []))
    expected_modify = {key: set(value) for key, value in (profile.get("modify") or {}).items()}
    for logical_id in sorted(set(added) - expected_add):
        problems.append(f"unexpected addition: {logical_id} ({added[logical_id]})")
    for logical_id in sorted(expected_add - set(added)):
        problems.append(f"expected addition missing: {logical_id}")
    expected_remove = set(profile.get("remove", []))
    for logical_id in sorted(set(removed) - expected_remove):
        problems.append(f"removal outside the profile: {logical_id}")
    for logical_id in sorted(expected_remove - set(removed)):
        problems.append(f"expected removal missing: {logical_id}")
    for logical_id, properties in sorted(modified.items()):
        if logical_id not in expected_modify:
            problems.append(f"unexpected modification: {logical_id} [{', '.join(properties)}]")
            continue
        extra = sorted(set(properties) - expected_modify[logical_id])
        if extra:
            problems.append(f"unexpected properties of {logical_id}: {', '.join(extra)}")
    for logical_id, status in sorted(replacements.items()):
        if status not in {"False", "no"}:
            problems.append(f"replacement risk ({status}): {logical_id}")
    allowed_iam = set(profile.get("iamResources", []))
    for logical_id in sorted(set(iam_resources) - allowed_iam):
        problems.append(f"IAM resource changed outside the profile: {logical_id}")
    return problems


def check_preview(preview: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    problems = _violations_for(
        profile,
        {item["id"]: item["type"] for item in preview["added"]},
        {item["id"]: item["properties"] for item in preview["modified"]},
        [item["id"] for item in preview["removed"]],
        # The preview cannot know the replacement behaviour of some types (the Harness); the profile
        # names them and the real change set must then show Replacement=False (check-change-set).
        {
            item["id"]: item["replacement"]
            for item in preview["modified"]
            if item["replacement"] != "no" and not (item["replacement"] == "UNKNOWN" and item["id"] in profile.get("previewReplacementUnknown", []))
        },
        sorted({item["resource"] for item in preview["iam"]}),
    )
    expected_iam = {(e["resource"], e["policy"], e["sid"], e["change"]) for e in profile.get("iam", [])}
    optional_iam = {(e["resource"], e["policy"], e["sid"], e["change"]) for e in profile.get("iam", []) if e.get("optional")}
    seen_iam = {(c["resource"], c["policy"], c["sid"], c["change"]) for c in preview["iam"]}
    for item in sorted(seen_iam - expected_iam):
        problems.append("unexpected IAM statement change: " + " / ".join(item))
    for item in sorted(expected_iam - seen_iam - optional_iam):
        problems.append("expected IAM statement change missing: " + " / ".join(item))
    allowed_wide = {(e["resource"], e["sid"]): set(e.get("reasons", [])) for e in profile.get("wideAllowed", [])}
    for change in preview["iam"]:
        if not change["wide"]:
            continue
        extra = set(change["wideReasons"]) - allowed_wide.get((change["resource"], change["sid"]), set())
        if extra:
            problems.append(f"widening not allowed by the profile in {change['resource']} / {change['sid']}: {', '.join(sorted(extra))}")
    for logical_id in preview["conditionUnknown"]:
        problems.append(f"condition of {logical_id} could not be resolved; pass the missing --param/--deployed-param")
    return problems


def check_change_set(described: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    """Validate the JSON of ``describe-change-set`` against a profile."""

    changes = described.get("Changes")
    if not isinstance(changes, list):
        raise PreviewError("change_set_shape_invalid")
    if described.get("Status") not in {None, "CREATE_COMPLETE"}:
        raise PreviewError("change_set_not_ready")
    added: dict[str, str] = {}
    modified: dict[str, list[str]] = {}
    removed: list[str] = []
    replacements: dict[str, str] = {}
    touched_iam: list[str] = []
    for wrapper in changes:
        change = wrapper.get("ResourceChange") if isinstance(wrapper, Mapping) else None
        if not isinstance(change, Mapping):
            raise PreviewError("change_set_shape_invalid")
        logical_id, action, resource_type = change.get("LogicalResourceId"), change.get("Action"), change.get("ResourceType")
        if not isinstance(logical_id, str) or not isinstance(resource_type, str):
            raise PreviewError("change_set_shape_invalid")
        if change.get("Replacement") not in (None, "False"):
            replacements[logical_id] = str(change.get("Replacement"))
        if resource_type in IAM_TYPES and action in {"Add", "Modify", "Remove"}:
            touched_iam.append(logical_id)
        if action == "Add":
            added[logical_id] = resource_type
        elif action == "Remove":
            removed.append(logical_id)
        elif action == "Modify":
            names = {
                detail["Target"]["Name"]
                for detail in change.get("Details") or []
                if isinstance(detail, Mapping) and isinstance(detail.get("Target"), Mapping) and isinstance(detail["Target"].get("Name"), str)
            }
            # A Modify with no named property (dependency re-evaluation) is surfaced, not hidden.
            modified[logical_id] = sorted(names) or ["<dependency re-evaluation>"]
        else:
            raise PreviewError("change_set_shape_invalid")
    problems = _violations_for(profile, added, modified, removed, replacements, touched_iam)
    for logical_id, properties in modified.items():
        if properties == ["<dependency re-evaluation>"] and logical_id in (profile.get("modify") or {}):
            problems.append(f"{logical_id} is modified without a named property; review it by hand")
    return problems


# --------------------------------------------------------------------------------- CLI


def _git_show(ref: str, path: str) -> str:
    if ref.startswith("-") or ".." in path:
        raise PreviewError("git_ref_invalid")
    try:
        done = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=True, timeout=30)  # noqa: S603,S607 - local read-only git
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreviewError("git_show_failed") from exc
    return done.stdout


def _render(preview: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"ADD      ({len(preview['added'])})")
    lines += [f"  + {item['id']}  {item['type']}" for item in preview["added"]]
    lines.append(f"MODIFY   ({len(preview['modified'])})")
    lines += [f"  ~ {item['id']}  {item['type']}  props=[{', '.join(item['properties'])}]  replacement={item['replacement']}" for item in preview["modified"]]
    lines.append(f"REMOVE   ({len(preview['removed'])})")
    lines += [f"  - {item['id']}  {item['type']}" for item in preview["removed"]]
    lines.append(f"IAM      ({len(preview['iam'])} statement-level changes)")
    lines += [f"  {item['change']:<7} {item['resource']} / {item['policy']} / {item['sid']}{'  [WIDE: ' + ', '.join(item['wideReasons']) + ']' if item['wide'] else ''}" for item in preview["iam"]]
    if preview["newParameters"]:
        lines.append("NEW PARAMETERS (must be supplied or have a default): " + ", ".join(preview["newParameters"]))
    if preview["removedParameters"]:
        lines.append("REMOVED PARAMETERS: " + ", ".join(preview["removedParameters"]))
    return "\n".join(lines)


def cd_template_match(deployed_text: str, local_text: str) -> bool:
    """Would the code-only CD accept this pair? It requires the two templates to be identical.

    Uses the CD's own loader and comparison, so this is the check that the next promotion will
    not stop at ``deployed_template_scope_changed``.
    """

    try:
        from scripts import deploy_release as cd
    except ImportError:  # run as `python scripts/phase15_changeset_preview.py`
        import deploy_release as cd  # type: ignore[no-redef]
    try:
        return bool(cd._templates_code_compatible(cd._load_yaml_text(local_text), cd._load_yaml_text(deployed_text)))
    except cd.DeploymentError as exc:
        raise PreviewError(str(exc)) from exc


def build_parameters(template: Mapping[str, Any], stack: Mapping[str, Any], overrides: Mapping[str, str]) -> list[dict[str, Any]]:
    """The ``--parameters`` list of a change set: only the named deltas change.

    A parameter named in ``overrides`` takes the new value; one the stack already has keeps
    ``UsePreviousValue`` (this is also what keeps ``NoEcho`` values, which describe-stacks masks,
    out of every file); a new parameter is omitted when the template gives it a default and is an
    error when it does not, so nothing is ever guessed.
    """

    declared = template.get("Parameters") or {}
    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise PreviewError("override_not_in_template:" + ",".join(unknown))
    existing = {item.get("ParameterKey") for item in stack.get("Parameters") or [] if isinstance(item, Mapping)}
    result: list[dict[str, Any]] = []
    for name in sorted(declared):
        if name in overrides:
            result.append({"ParameterKey": name, "ParameterValue": str(overrides[name])})
        elif name in existing:
            result.append({"ParameterKey": name, "UsePreviousValue": True})
        elif isinstance(declared[name], Mapping) and "Default" in declared[name]:
            continue
        else:
            raise PreviewError("parameter_value_missing:" + name)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="compare a deployed template with the repository template")
    source = preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--deployed", type=Path, help="backup of the deployed template (aws cloudformation get-template)")
    source.add_argument("--deployed-git-ref", help="rehearsal only: take the deployed template from this git ref (for example origin/prod)")
    preview.add_argument("--local", type=Path, required=True)
    preview.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="parameter value of the new deployment (repeatable)")
    preview.add_argument("--deployed-param", action="append", default=[], metavar="NAME=VALUE", help="parameter value currently deployed (repeatable)")
    preview.add_argument("--profile", help="also check the preview against this expectation profile")
    preview.add_argument("--json", action="store_true")
    params = sub.add_parser("params", help="write the --parameters JSON of a change set from a describe-stacks backup")
    params.add_argument("--stack-json", type=Path, required=True, help="saved `aws cloudformation describe-stacks --stack-name X` output")
    params.add_argument("--template", type=Path, required=True, help="the repository template that will be deployed")
    params.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="the parameter deltas of this step (repeatable)")
    params.add_argument("--output", type=Path, required=True, help="file to write; it holds no secret")
    match = sub.add_parser("cd-template-match", help="check that the deployed Public Edge template equals the repository one, as the code-only CD requires")
    match.add_argument("--deployed", type=Path, required=True)
    match.add_argument("--local", type=Path, default=Path("infra/cloudformation/phase-14-public-edge.yaml"))
    check = sub.add_parser("check-change-set", help="check a saved describe-change-set JSON against a profile")
    check.add_argument("--change-set-json", type=Path, required=True)
    check.add_argument("--profile", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "params":
            stacks = json.loads(args.stack_json.read_text(encoding="utf-8")).get("Stacks")
            if not isinstance(stacks, list) or len(stacks) != 1:
                raise PreviewError("stack_inventory_invalid")
            built = build_parameters(load_template(args.template.read_text(encoding="utf-8")), stacks[0], _parse_params(args.set))
            args.output.write_text(json.dumps(built, indent=2) + chr(10), encoding="utf-8", newline=chr(10))
            print(json.dumps({"parameters": len(built), "changed": sorted(_parse_params(args.set)), "output": str(args.output)}))
            return 0
        if args.command == "cd-template-match":
            matches = cd_template_match(args.deployed.read_text(encoding="utf-8"), args.local.read_text(encoding="utf-8"))
            print(json.dumps({"cdTemplateMatch": matches}))
            return 0 if matches else 1
        if args.command == "check-change-set":
            described = json.loads(args.change_set_json.read_text(encoding="utf-8"))
            problems = check_change_set(described, load_profile(args.profile))
            print(json.dumps({"profile": args.profile, "ok": not problems, "violations": problems}, indent=2))
            return 1 if problems else 0
        local = load_template(args.local.read_text(encoding="utf-8"))
        if args.deployed_git_ref:
            deployed_text = _git_show(args.deployed_git_ref, args.local.as_posix())
        else:
            deployed_text = args.deployed.read_text(encoding="utf-8")
        deployed = load_template(deployed_text)
        preview = compare(deployed, local, _parse_params(args.deployed_param), _parse_params(args.param))
        problems = check_preview(preview, load_profile(args.profile)) if args.profile else []
        print(json.dumps({"preview": preview, "violations": problems}, indent=2) if args.json else _render(preview))
        if args.profile and not args.json:
            print("PROFILE " + args.profile + ": " + ("OK" if not problems else "VIOLATIONS"))
            print("\n".join("  ! " + problem for problem in problems))
        return 1 if problems else 0
    except PreviewError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
