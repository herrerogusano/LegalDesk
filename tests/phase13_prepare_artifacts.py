"""Operator preparation: local ZIPs and explicit read-only template backups.

Never deploys, uploads, infers, creates credentials, or logs request bodies.
Backups under ignored build/ permit restoration of the three shared stacks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "build" / "phase13-smoke"
STACKS = ("LegalDeskPhase02Documents", "LegalDeskPhase07ReviewTask", "LegalDeskPhase08Gateway")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-aws-templates", action="store_true")
    args = parser.parse_args()
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if args.backup_aws_templates:
        import boto3
        from botocore.config import Config
        client = boto3.client("cloudformation", region_name="eu-west-1", config=Config(retries={"total_max_attempts": 1}))
        for name in STACKS:
            target = DIRECTORY / f"{name}.original.json"
            if target.exists():
                raise RuntimeError("Refusing to overwrite an original template backup")
            template = client.get_template(StackName=name, TemplateStage="Original")["TemplateBody"]
            value = template if isinstance(template, str) else json.dumps(template, indent=2)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
            print(json.dumps({"backup": name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}))
    else:
        sources = sorted((ROOT / "backend" / "src" / "legaldesk").rglob("*.py"))
        for name in ("review-task", "interceptor", "metadata-mcp"):
            target = DIRECTORY / f"{name}.zip"
            with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
                for source in sources:
                    entry = ZipInfo(source.relative_to(ROOT / "backend" / "src").as_posix(), date_time=(2026, 9, 21, 0, 0, 0))
                    entry.compress_type = ZIP_DEFLATED
                    archive.writestr(entry, source.read_bytes())
            with ZipFile(target) as archive:
                assert archive.testzip() is None
                assert "legaldesk/review_tasks.py" in archive.namelist()
                assert all(name.endswith(".py") for name in archive.namelist())
            print(json.dumps({"artifact": name, "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
