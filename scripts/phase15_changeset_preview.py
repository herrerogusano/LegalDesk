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
    modifications and properties, no removal unless the profile lists it, and no replacement.

    Two refinements learned from the real change sets of the T9 live block (2026-11-03):

    * a property that changes only because a resource **of the same change set that is not
      replaced** (``Replacement=False``) changed an attribute (``Evaluation: Dynamic`` +
      ``ChangeSource: ResourceAttribute`` + ``CausingEntity`` = that resource), such as the
      ``RoleArn`` of the Gateway or the ``ExecutionRoleArn`` of the Harness, is inherited, not
      an edit, and is accepted without being listed. Any other property still has to be listed;
    * ``Replacement: Conditional`` is not a blind stop: the properties that make it conditional
      are looked up in the createOnly list of the resource type (``createOnly`` in the expectations
      file, from ``aws cloudformation describe-type``). A createOnly property stops; a property
      with createOnly paths *below* it (``Memory/ManagedMemoryConfiguration/EncryptionKeyArn``)
      stops unless the profile accepts it explicitly (``conditionalNestedAllowed``); a type
      without a list stops.

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


def load_create_only(path: Path = EXPECTATIONS_PATH) -> dict[str, tuple[str, ...]]:
    """createOnly property paths per resource type (from ``describe-type``), as recorded in the expectations file."""

    try:
        table = json.loads(path.read_text(encoding="utf-8-sig")).get("createOnly", {})
    except (OSError, ValueError) as exc:
        raise PreviewError("profile_unknown") from exc
    return {str(name): tuple(str(item) for item in paths) for name, paths in table.items() if isinstance(paths, list)}


def replacement_of(resource_type: str, properties: Sequence[str], table: Mapping[str, Sequence[str]]) -> tuple[str, list[str]]:
    """(status, properties whose nested paths are createOnly) for a set of changed top-level properties.

    ``REPLACEMENT``: a changed property is itself createOnly. ``CONDITIONAL``: a changed property has createOnly paths
    below it, so a replacement is possible depending on the value. ``no``: nothing createOnly is involved. ``UNKNOWN``:
    the type has no createOnly list (and no heuristic).
    """

    paths = table.get(resource_type)
    if paths is None:
        immutable = REPLACEMENT_PROPERTIES.get(resource_type)
        if immutable is None:
            return "UNKNOWN", []
        return ("REPLACEMENT" if immutable.intersection(properties) else "no"), []
    whole = {path for path in paths if "/" not in path}
    if whole.intersection(properties):
        return "REPLACEMENT", []
    nested = sorted(name for name in properties if any(path.startswith(name + "/") for path in paths))
    return ("CONDITIONAL" if nested else "no"), nested


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


def _replacement(resource_type: str, properties: Sequence[str], table: Mapping[str, Sequence[str]] | None = None) -> tuple[str, list[str]]:
    return replacement_of(resource_type, properties, load_create_only() if table is None else table)


def compare(deployed: Mapping[str, Any], local: Mapping[str, Any], deployed_params: Mapping[str, str], local_params: Mapping[str, str]) -> dict[str, Any]:
    """The preview: added / modified / removed resources, replacement risk and IAM changes."""

    create_only = load_create_only()
    old = effective_resources(deployed, Context(deployed, deployed_params))
    new = effective_resources(local, Context(local, local_params))
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified: dict[str, dict[str, Any]] = {}
    for logical_id in sorted(set(old) & set(new)):
        before, after = old[logical_id], new[logical_id]
        if before["type"] != after["type"]:
            modified[logical_id] = {"type": after["type"], "properties": ["<Type>"], "replacement": "REPLACEMENT", "conditionalProperties": []}
            continue
        names = sorted(
            name for name in set(before["properties"]) | set(after["properties"])
            if _canon(before["properties"].get(name)) != _canon(after["properties"].get(name))
        )
        if names:
            status, conditional = _replacement(after["type"], names, create_only)
            modified[logical_id] = {"type": after["type"], "properties": names, "replacement": status, "conditionalProperties": conditional}
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
        profiles = json.loads(path.read_text(encoding="utf-8-sig"))["profiles"]
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
        # CONDITIONAL (a changed property has createOnly paths below it) is accepted only when the profile names it.
        {
            item["id"]: item["replacement"]
            for item in preview["modified"]
            if item["replacement"] != "no" and not (
                item["replacement"] == "CONDITIONAL" and set(item["conditionalProperties"]) <= set((profile.get("conditionalNestedAllowed") or {}).get(item["id"], {}))
            )
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


def _causing_root(entity: object) -> str | None:
    """``GatewayExecutionRole.Arn`` -> ``GatewayExecutionRole`` (a parameter name has no dot and is returned as is)."""

    return entity.split(".", 1)[0] if isinstance(entity, str) and entity else None


def analyze_change_set(described: Mapping[str, Any], profile: Mapping[str, Any], create_only: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Validate the JSON of ``describe-change-set`` against a profile.

    Returns ``{"violations": [...], "inherited": [...], "conditionalAccepted": [...]}``: what was refused, and what
    was accepted by rule rather than by being listed (so that the operator sees it).
    """

    table = load_create_only() if create_only is None else create_only
    changes = described.get("Changes")
    if not isinstance(changes, list):
        raise PreviewError("change_set_shape_invalid")
    if described.get("Status") not in {None, "CREATE_COMPLETE"}:
        raise PreviewError("change_set_not_ready")
    rows: list[Mapping[str, Any]] = []
    for wrapper in changes:
        change = wrapper.get("ResourceChange") if isinstance(wrapper, Mapping) else None
        if not isinstance(change, Mapping):
            raise PreviewError("change_set_shape_invalid")
        if not isinstance(change.get("LogicalResourceId"), str) or not isinstance(change.get("ResourceType"), str):
            raise PreviewError("change_set_shape_invalid")
        if change.get("Action") not in {"Add", "Modify", "Remove"}:
            raise PreviewError("change_set_shape_invalid")
        rows.append(change)
    # A resource of this change set that is modified and NOT replaced: what an inherited change may point at.
    stable = {row["LogicalResourceId"] for row in rows if row["Action"] == "Modify" and row.get("Replacement") == "False"}

    added: dict[str, str] = {}
    modified: dict[str, list[str]] = {}
    removed: list[str] = []
    replacements: dict[str, str] = {}
    touched_iam: list[str] = []
    inherited: list[str] = []
    conditional_accepted: list[str] = []
    extra_problems: list[str] = []
    allowed_nested = profile.get("conditionalNestedAllowed") or {}
    for change in rows:
        logical_id, action, resource_type = change["LogicalResourceId"], change["Action"], change["ResourceType"]
        # Added, removed or replaced IAM resources always count; a replacement gets a new ARN whatever the details say.
        if resource_type in IAM_TYPES and (action in ("Add", "Remove") or change.get("Replacement") not in (None, "False")):
            touched_iam.append(logical_id)
        if action == "Add":
            added[logical_id] = resource_type
        elif action == "Remove":
            removed.append(logical_id)
        else:
            details = [d for d in change.get("Details") or [] if isinstance(d, Mapping) and isinstance(d.get("Target"), Mapping) and isinstance(d["Target"].get("Name"), str)]
            direct: set[str] = set()
            for detail in details:
                name = detail["Target"]["Name"]
                cause = _causing_root(detail.get("CausingEntity"))
                if detail.get("Evaluation") == "Dynamic" and detail.get("ChangeSource") == "ResourceAttribute" and cause in stable and cause != logical_id:
                    inherited.append(f"{logical_id}.{name} <- {detail.get('CausingEntity')}")
                else:
                    direct.add(name)
            # Nothing named at all (dependency re-evaluation) is surfaced; an all-inherited resource needs no listing.
            if direct or not details:
                modified[logical_id] = sorted(direct) or ["<dependency re-evaluation>"]
                # An IAM resource counts as changed only through a direct change: one whose only change is inherited from a
                # resource of this change set that is not replaced keeps exactly the same policy text (2026-11-03, step 9 again:
                # ProductionDeploymentRole.Policies <- CloudFormationExecutionRole.Arn).
                if resource_type in IAM_TYPES and logical_id not in touched_iam:
                    touched_iam.append(logical_id)
        replacement = change.get("Replacement")
        if replacement in (None, "False"):
            continue
        if replacement != "Conditional":
            replacements[logical_id] = str(replacement)
            continue
        paths = table.get(resource_type)
        if paths is None:
            extra_problems.append(f"replacement risk (Conditional): {logical_id}: no createOnly list for {resource_type} in the expectations file; stop")
            continue
        always = [d["Target"]["Name"] for d in change.get("Details") or [] if isinstance(d, Mapping) and isinstance(d.get("Target"), Mapping)
                  and d["Target"].get("RequiresRecreation") == "Always"]
        for name in always:
            extra_problems.append(f"replacement risk (Conditional): {logical_id}.{name} always requires recreation")
        conditional = []
        explained_by_inheritance = False
        for detail in change.get("Details") or []:
            if not (isinstance(detail, Mapping) and isinstance(detail.get("Target"), Mapping) and detail["Target"].get("RequiresRecreation") == "Conditionally"):
                continue
            cause = _causing_root(detail.get("CausingEntity"))
            if detail.get("Evaluation") == "Dynamic" and detail.get("ChangeSource") == "ResourceAttribute" and cause in stable and cause != logical_id:
                # A createOnly property whose value comes from an attribute of a resource that this same change set modifies IN PLACE (not
                # replaced): its ARN/id cannot change, so the property cannot change and the conditional replacement cannot happen
                # (verified live 2026-11-03: AWS::Lambda::Permission.SourceArn <- the schedule's Arn, permission not replaced). Surfaced, not silent.
                # (Already listed under ``inherited`` by the pass above, so it is surfaced to the operator, not silent.)
                explained_by_inheritance = True
                continue
            conditional.append(detail["Target"]["Name"])
        if not conditional and not always and not explained_by_inheritance:
            extra_problems.append(f"replacement risk (Conditional): {logical_id}: no property explains it; stop")
        for name in conditional:
            whole = name in paths
            nested = [path for path in paths if path.startswith(name + "/")]
            if whole:
                extra_problems.append(f"replacement risk (Conditional): {logical_id}.{name} is createOnly")
            elif not nested:
                extra_problems.append(f"replacement risk (Conditional): {logical_id}.{name} has no createOnly path in the list for {resource_type}: the list is stale or incomplete; stop")
            elif name in allowed_nested.get(logical_id, {}):
                conditional_accepted.append(f"{logical_id}.{name}: {allowed_nested[logical_id][name]}")
            else:
                extra_problems.append(
                    f"replacement risk (Conditional): {logical_id}.{name} has createOnly paths below it ({', '.join(nested)}); "
                    "accept it in the profile (conditionalNestedAllowed) only after checking that none of them changes"
                )
    problems = _violations_for(profile, added, modified, removed, replacements, touched_iam) + extra_problems
    for logical_id, properties in modified.items():
        if properties == ["<dependency re-evaluation>"] and logical_id in (profile.get("modify") or {}):
            problems.append(f"{logical_id} is modified without a named property; review it by hand")
    return {"violations": problems, "inherited": inherited, "conditionalAccepted": conditional_accepted}


def check_change_set(described: Mapping[str, Any], profile: Mapping[str, Any], create_only: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    return analyze_change_set(described, profile, create_only)["violations"]


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
    lines += [f"  ~ {item['id']}  {item['type']}  props=[{', '.join(item['properties'])}]  replacement={item['replacement']}{' (' + ', '.join(item['conditionalProperties']) + ')' if item['conditionalProperties'] else ''}" for item in preview["modified"]]
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


BOOTSTRAP_STAGES = ("code-and-quota", "agent-chat")

# --- names that CloudFormation generates ------------------------------------------------------------
GENERATED_SUFFIX_LENGTH = 12
IAM_ROLE_NAME_LIMIT = 64


def cfn_generated_name(stack: str, logical_id: str, suffix: str, limit: int = IAM_ROLE_NAME_LIMIT) -> str:
    """The physical name CloudFormation generates for an unnamed resource: ``<stack>-<logical id>-<suffix>``.

    The rule is INFERRED from two names observed in the T9 live block of 2026-11-03, not read from AWS documentation:

    * ``LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr``: stack 26 + logical id 24 + 12 = 64, nothing cut;
    * ``LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-bzFuZq7zur6f``: stack 26 -> 25 and logical id 28 -> 25 + 12 = 64:
      **both parts are cut**, not only the logical id as first assumed (that wrong assumption denied ``iam:CreateRole``).

    Both fit "the first two parts share a budget of ``limit - 14`` characters; if they do not fit, each is cut to half of it,
    and a part shorter than half keeps its length while the other takes the rest". The suffix is random: callers must treat only the part
    before it as stable. The limit is that of the resource type (IAM role 64, CloudWatch alarm 255): a long limit cuts nothing.
    """

    budget = limit - len(suffix) - 2
    if len(stack) + len(logical_id) > budget:
        half = budget // 2
        if len(stack) <= half:
            logical_id = logical_id[: budget - len(stack)]
        elif len(logical_id) <= half:
            stack = stack[: budget - len(logical_id)]
        else:
            stack, logical_id = stack[:half], logical_id[:half]
    return f"{stack}-{logical_id}-{suffix}"


def bootstrap_parameters(resources: Mapping[str, Any], stack: Mapping[str, Any], stage: str) -> list[dict[str, str]]:
    """Parameters of the temporary Public Edge role stack, derived from the deployed stack, never typed by hand.

    ``resources`` is ``aws cloudformation describe-stack-resources`` of ``LegalDeskPhase14PublicEdge`` and ``stack`` its
    ``describe-stacks``. Account, region and partition come from the stack id; every ARN is rebuilt from a physical id.
    """

    if stage not in BOOTSTRAP_STAGES:
        raise PreviewError("stage_invalid")
    stack_id = str(stack.get("StackId", ""))
    parts = stack_id.split(":")
    if len(parts) < 6 or parts[0] != "arn" or not parts[4].isdigit() or stack.get("StackName") != "LegalDeskPhase14PublicEdge":
        raise PreviewError("stack_inventory_invalid")
    partition, region, account = parts[1], parts[3], parts[4]
    physical: dict[str, str] = {}
    for item in resources.get("StackResources") or []:
        if isinstance(item, Mapping) and isinstance(item.get("LogicalResourceId"), str) and isinstance(item.get("PhysicalResourceId"), str):
            physical[item["LogicalResourceId"]] = item["PhysicalResourceId"]
    needed = ("ApplicationExecutionRole", "ApplicationFunction", "PublicDistribution", "FrontendOriginAccessControl", "EdgeResponseHeadersPolicy", "PublicApi", "PublicApiIntegration")
    missing = [name for name in needed if not physical.get(name)]
    if missing:
        raise PreviewError("resource_missing:" + ",".join(missing))
    bucket = next((item.get("ParameterValue") for item in stack.get("Parameters") or [] if isinstance(item, Mapping) and item.get("ParameterKey") == "ApplicationCodeBucket"), None)
    if not isinstance(bucket, str) or not bucket or "/" in bucket:
        raise PreviewError("artifact_bucket_missing")
    values = {
        "Stage": stage,
        "ExistingApplicationRoleArn": f"arn:{partition}:iam::{account}:role/{physical['ApplicationExecutionRole']}",
        "ApplicationFunctionArn": f"arn:{partition}:lambda:{region}:{account}:function:{physical['ApplicationFunction']}",
        "CloudFrontDistributionArn": f"arn:{partition}:cloudfront::{account}:distribution/{physical['PublicDistribution']}",
        "OriginAccessControlArn": f"arn:{partition}:cloudfront::{account}:origin-access-control/{physical['FrontendOriginAccessControl']}",
        "ResponseHeadersPolicyArn": f"arn:{partition}:cloudfront::{account}:response-headers-policy/{physical['EdgeResponseHeadersPolicy']}",
        "PublicApiIntegrationArn": f"arn:{partition}:apigateway:{region}::/apis/{physical['PublicApi']}/integrations/{physical['PublicApiIntegration']}",
        "ArtifactBucketArn": f"arn:{partition}:s3:::{bucket}",
    }
    return [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]


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
    boot = sub.add_parser("bootstrap-params", help="write the parameters of the temporary Public Edge role stack from the deployed stack")
    boot.add_argument("--stack-json", type=Path, required=True, help="saved `describe-stacks --stack-name LegalDeskPhase14PublicEdge`")
    boot.add_argument("--resources-json", type=Path, required=True, help="saved `describe-stack-resources --stack-name LegalDeskPhase14PublicEdge`")
    boot.add_argument("--stage", choices=BOOTSTRAP_STAGES, required=True)
    boot.add_argument("--output", type=Path, required=True)
    gen = sub.add_parser("generated-name", help="the name CloudFormation will generate for an unnamed resource (the rule of cfn_generated_name), with a placeholder suffix")
    gen.add_argument("--stack", required=True)
    gen.add_argument("--logical-id", required=True)
    gen.add_argument("--limit", type=int, default=IAM_ROLE_NAME_LIMIT, help="64 for an IAM role (default), 255 for a CloudWatch alarm")
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
        if args.command == "generated-name":
            print(cfn_generated_name(args.stack, args.logical_id, "X" * GENERATED_SUFFIX_LENGTH, args.limit))  # only the part before the suffix is stable
            return 0
        if args.command == "bootstrap-params":
            stacks = json.loads(args.stack_json.read_text(encoding="utf-8-sig")).get("Stacks")
            if not isinstance(stacks, list) or len(stacks) != 1:
                raise PreviewError("stack_inventory_invalid")
            built = bootstrap_parameters(json.loads(args.resources_json.read_text(encoding="utf-8-sig")), stacks[0], args.stage)
            args.output.write_text(json.dumps(built, indent=2) + chr(10), encoding="utf-8", newline=chr(10))
            print(json.dumps({"parameters": len(built), "stage": args.stage, "output": str(args.output)}))
            return 0
        if args.command == "params":
            stacks = json.loads(args.stack_json.read_text(encoding="utf-8-sig")).get("Stacks")
            if not isinstance(stacks, list) or len(stacks) != 1:
                raise PreviewError("stack_inventory_invalid")
            built = build_parameters(load_template(args.template.read_text(encoding="utf-8-sig")), stacks[0], _parse_params(args.set))
            args.output.write_text(json.dumps(built, indent=2) + chr(10), encoding="utf-8", newline=chr(10))
            print(json.dumps({"parameters": len(built), "changed": sorted(_parse_params(args.set)), "output": str(args.output)}))
            return 0
        if args.command == "cd-template-match":
            matches = cd_template_match(args.deployed.read_text(encoding="utf-8-sig"), args.local.read_text(encoding="utf-8-sig"))
            print(json.dumps({"cdTemplateMatch": matches}))
            return 0 if matches else 1
        if args.command == "check-change-set":
            described = json.loads(args.change_set_json.read_text(encoding="utf-8-sig"))
            analysis = analyze_change_set(described, load_profile(args.profile))
            problems = analysis["violations"]
            print(json.dumps({"profile": args.profile, "ok": not problems, "violations": problems, "acceptedAsInherited": analysis["inherited"],
                              "acceptedAsConditional": analysis["conditionalAccepted"]}, indent=2))
            return 1 if problems else 0
        local = load_template(args.local.read_text(encoding="utf-8-sig"))
        if args.deployed_git_ref:
            deployed_text = _git_show(args.deployed_git_ref, args.local.as_posix())
        else:
            deployed_text = args.deployed.read_text(encoding="utf-8-sig")
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
