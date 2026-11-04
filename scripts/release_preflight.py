"""Read-only Public Edge template gate before opening a release PR. No change set or write."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.deploy_release import STACK_NAME, _load_yaml, _load_yaml_text, _templates_code_compatible
except ImportError:
    from deploy_release import STACK_NAME, _load_yaml, _load_yaml_text, _templates_code_compatible

ROOT = Path(__file__).resolve().parents[1]


def compare(local: Mapping[str, Any], deployed: Mapping[str, Any]) -> dict[str, Any]:
    """Use the exact CD comparator; print names only, including description-only drift."""
    differences = {}
    for section in ("Parameters", "Resources"):
        before, after = deployed.get(section, {}), local.get(section, {})
        differences[section] = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    differences["otherSections"] = sorted(k for k in set(local) | set(deployed)
                                          if k not in {"Parameters", "Resources"} and local.get(k) != deployed.get(k))
    return {"compatible": _templates_code_compatible(local, deployed), "differences": differences}


def preflight(client: Any, template: Path) -> dict[str, Any]:
    local = _load_yaml(template)
    response = client.get_template(StackName=STACK_NAME, TemplateStage="Original")
    body = response["TemplateBody"]
    deployed = _load_yaml_text(body) if isinstance(body, str) else body
    if not isinstance(deployed, Mapping):
        raise ValueError("deployed_template_invalid")
    return {"stack": STACK_NAME, "templateStage": "Original", **compare(local, deployed)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--profile")
    parser.add_argument("--template", type=Path, default=ROOT / "infra/cloudformation/phase-14-public-edge.yaml")
    args = parser.parse_args(argv)
    import boto3
    from botocore.config import Config
    client = boto3.Session(profile_name=args.profile, region_name=args.region).client(
        "cloudformation", config=Config(retries={"total_max_attempts": 1}, read_timeout=30, connect_timeout=5))
    try:
        result = preflight(client, args.template)
    except Exception:
        print(json.dumps({"compatible": False, "error": "preflight_read_failed"}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
