"""One explicitly authorized real-browser smoke; never a default test.

Operator credentials provision/clean synthetic fixtures only. The application
receives an STS restricted session. Credentials/passwords stay in memory.
Infrastructure teardown is separate and must follow the execution ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT / "agent/src")]
REGION = "eu-west-1"
ACCOUNT = "344774635844"
TENANT = "tnt_phase13_20260921"
MATTERS = ("mat_phase13_a_20260921", "mat_phase13_b_20260921")
TABLE = "LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T"
BUCKET = "legaldeskphase02documents-documentbucket-ojlu4kvhlied"
POOL = "eu-west-1_tHvFPpktv"
MEMORY = "LegalDeskPhase09-NKV8SZFz5U"
HARNESS = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/LegalDeskPhase01-7EMjvNs1PC"
MODEL = "eu.anthropic.claude-sonnet-4-6"
REPORT = ROOT / "build/phase13-smoke/live-result.json"


def session_policy(subjects):
    """Narrow resource/action intersections; never IAM/control-plane authority."""
    bedrock = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:"
    agentcore = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    def allow(actions, resources, **extra):
        return dict(Effect="Allow", Action=actions, Resource=resources, **extra)
    statements = [
        allow(["s3:PutObject", "s3:GetObject"], f"arn:aws:s3:::{BUCKET}/tenants/{TENANT}/matters/*/documents/*"),
        allow(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
              f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}",
              Condition={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [
                  *[f"AUTH#USER#{sub}" for sub in subjects], *[f"AUTH#MATTER#{m}" for m in MATTERS],
                  f"TENANT#{TENANT}#MATTER#*", f"CONVERSATION#{TENANT}#*", "GATEWAY#INVOCATION#*", "GATEWAY#GRANT#*",
              ]}}),
        allow(["bedrock:Retrieve", "bedrock:StartIngestionJob", "bedrock:GetIngestionJob"], bedrock + "knowledge-base/40R8OKAZOR"),
        allow("bedrock:ApplyGuardrail", bedrock + "guardrail/qin0b7t7vmtd"),
        allow("bedrock:InvokeModel", [bedrock + "inference-profile/" + MODEL,
              "arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-sonnet-4-6"]),
        allow(["bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"], HARNESS),
        allow(["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents"], agentcore + "memory/" + MEMORY),
    ]
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


class TrackingProxy:
    def __init__(self, delegate, state, kind):
        self.delegate, self.state, self.kind = delegate, state, kind

    def __getattr__(self, name):
        method = getattr(self.delegate, name)
        if not callable(method):
            return method
        def call(*args, **kwargs):
            if self.kind == "table" and name in {"put_item", "update_item"}:
                item = kwargs.get("Item", kwargs.get("Key", {}))
                self.state["keys"].add((item["pk"], item["sk"]))
            if self.kind == "s3":
                params = kwargs.get("Params", kwargs)
                if params.get("Key"):
                    self.state["objects"].add(params["Key"])
            if self.kind == "bedrock-agentcore":
                actor = kwargs.get("actorId")
                session = kwargs.get("sessionId", kwargs.get("runtimeSessionId"))
                if actor and session:
                    self.state["memory_scopes"].add((actor, session))
            return method(*args, **kwargs)
        return call


class TrackingSession:
    def __init__(self, session, state):
        self.session, self.state = session, state

    def client(self, service, **kwargs):
        return TrackingProxy(self.session.client(service, **kwargs), self.state, service)

    def resource(self, service, **kwargs):
        resource = self.session.resource(service, **kwargs)
        state = self.state
        class Resource:
            def Table(self, name):
                return TrackingProxy(resource.Table(name), state, "table")
        return Resource()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-approved-once", action="store_true")
    args = parser.parse_args()
    policy = session_policy(["a" * 36, "b" * 36])
    if len(policy) > 2048:
        raise RuntimeError("Session policy exceeds STS inline limit")
    pdf = ROOT / "output/pdf/fictional-storage-note.pdf"
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != "03de0479544250393d9709a1eb4511a1e24332871f06252b8cb4513398d0c17a":
        raise RuntimeError("Frozen smoke PDF has changed")
    if not args.execute_approved_once:
        print(json.dumps({"mode": "LOCAL_PREFLIGHT", "policyCharacters": len(policy), "awsCalls": 0}))
        return 0
    if REPORT.exists():
        raise RuntimeError("Refusing to repeat the authorized smoke")
    import boto3
    from boto3.dynamodb.conditions import Attr, Key
    from botocore.config import Config
    from legaldesk.application import AWSResourceConfig, build_aws_composition
    from legaldesk.__main__ import QuietRequestHandler
    from legaldesk.http_app import create_http_app
    from legaldesk.smoke_budget import SmokeBudget
    cfg = Config(retries={"total_max_attempts": 1, "mode": "standard"})
    operator = boto3.Session(region_name=REGION)
    cognito = operator.client("cognito-idp", config=cfg)
    table = operator.resource("dynamodb", config=cfg).Table(TABLE)
    s3 = operator.client("s3", config=cfg)
    core = operator.client("bedrock-agentcore", config=cfg)
    budget = SmokeBudget()
    state = {"keys": set(), "objects": set(), "memory_scopes": set()}
    created_users, subjects, passwords = [], [], []
    server = thread = None
    result = {"status": "STARTED", "cleanupErrors": [], "checks": []}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive sentinel prevents even an interrupted invocation being rerun.
    with REPORT.open("x", encoding="utf-8") as stream:
        json.dump(result, stream)
    def save():
        result["budget"] = asdict(budget.snapshot())
        result["syntheticUsers"] = list(created_users)
        result["cleanupKeys"] = [dict(pk=pk, sk=sk) for pk, sk in sorted(state["keys"])]
        result["objectKeys"] = sorted(state["objects"])
        result["memoryScopes"] = sorted(state["memory_scopes"])
        REPORT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    try:
        for i, suffix in enumerate(("a", "b")):
            username = f"phase13-smoke-{suffix}-20260921"
            password = "Q!8a" + secrets.token_urlsafe(24)
            response = cognito.admin_create_user(UserPoolId=POOL, Username=username, MessageAction="SUPPRESS", TemporaryPassword=password,
                UserAttributes=[{"Name": "email", "Value": username + "@example.invalid"}, {"Name": "email_verified", "Value": "true"}])
            created_users.append(username)
            sub = next(item["Value"] for item in response["User"]["Attributes"] if item["Name"] == "sub")
            subjects.append(sub)
            passwords.append(password)
            save()
            cognito.admin_set_user_password(UserPoolId=POOL, Username=username, Password=password, Permanent=True)
            user_id = f"usr_phase13_{suffix}_20260921"
            items = [
                dict(pk=f"AUTH#USER#{sub}", sk="PROFILE", entityType="User", userId=user_id, verifiedSubject=sub, tenantIds=[TENANT], roles=["member"]),
                dict(pk=f"AUTH#MATTER#{MATTERS[i]}", sk="PROFILE", entityType="Matter", matterId=MATTERS[i], tenantId=TENANT, name=f"Synthetic smoke {suffix}", authorizedUserIds=[user_id], status="active"),
            ]
            for item in items:
                budget.call("dynamodb", table.put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
                state["keys"].add((item["pk"], item["sk"]))
                save()
        policy = session_policy(subjects)
        if len(policy) > 2048:
            raise RuntimeError("Session policy exceeds limit")
        credentials = operator.client("sts", config=cfg).get_federation_token(Name="LegalDeskPhase13Smoke", DurationSeconds=3600, Policy=policy)["Credentials"]
        restricted = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"], region_name=REGION)
        del credentials
        issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
        domain = "https://legaldesk-phase08-344774635844.auth.eu-west-1.amazoncognito.com"
        config = AWSResourceConfig(region=REGION, metadata_table_name=TABLE, source_bucket_name=BUCKET,
            knowledge_base_id="40R8OKAZOR", data_source_id="A53UDNNEMP", guardrail_identifier="qin0b7t7vmtd", guardrail_version="1",
            resolver_model_id=MODEL, writer_model_id=MODEL, harness_arn=HARNESS,
            gateway_url="https://legaldeskgatewayphase08-f17ddi2woq.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp",
            memory_id=MEMORY, issuer=issuer, jwks_url=issuer + "/.well-known/jwks.json", client_id="101hke40t7n5easmh7i3g9265o",
            authorization_endpoint=domain + "/oauth2/authorize", token_endpoint=domain + "/oauth2/token", matter_catalog=MATTERS)
        composition = build_aws_composition(allow_aws=True, config=config, smoke_budget=budget, boto3_session=TrackingSession(restricted, state))
        app = create_http_app(composition)
        server = make_server("localhost", 8000, app, handler_class=QuietRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        child_env = dict(os.environ, LEGALDESK_SMOKE_USERNAME=created_users[0], LEGALDESK_SMOKE_PASSWORD=passwords[0])
        # Never pass the operator's credential environment to the browser process.
        for key in list(child_env):
            if key.startswith("AWS_"):
                child_env.pop(key)
        child = subprocess.run(["node", str(ROOT / "tests/phase13_live_browser.cjs")], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=750)
        del child_env
        # Browser runner emits only a closed, metadata-only JSON report.
        browser_report = json.loads(child.stdout.strip().splitlines()[-1])
        result["browser"] = browser_report
        result["status"] = "PASS" if child.returncode == 0 and browser_report.get("result") == "PASS" else "FAILED"
        if result["status"] == "PASS":
            response = budget.call("dynamodb", table.query, KeyConditionExpression=Key("pk").eq(f"TENANT#{TENANT}#MATTER#{MATTERS[0]}"), Limit=20)
            reviews = [item for item in response["Items"] if str(item.get("sk", "")).startswith("REVIEW#")]
            result["reviewVerified"] = len(reviews) == 1 and reviews[0].get("createdByUserId") == "usr_phase13_a_20260921" and reviews[0].get("status") == "open"
            if not result["reviewVerified"]:
                result["status"] = "FAILED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["errorType"] = type(exc).__name__
        if hasattr(exc, "response"):
            result["awsErrorCode"] = exc.response.get("Error", {}).get("Code")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        save()
        # Cleanup must run even when the smoke budget is halted. Its requests
        # are counted separately and target only recorded synthetic resources.
        def cleanup(label, fn, **kwargs):
            result["cleanupRequests"] = result.get("cleanupRequests", 0) + 1
            if result["cleanupRequests"] > 80:
                result["cleanupErrors"].append("cleanup_request_limit")
                return None
            try:
                return fn(**kwargs)
            except Exception as exc:
                result["cleanupErrors"].append(label + ":" + type(exc).__name__)
                return None
        for actor, session in sorted(state["memory_scopes"]):
            events = cleanup("list_events", core.list_events, memoryId=MEMORY, actorId=actor, sessionId=session, maxResults=100)
            if events:
                if events.get("nextToken"):
                    result["cleanupErrors"].append("memory_pagination")
                for event in events.get("events", []):
                    cleanup("delete_event", core.delete_event, memoryId=MEMORY, actorId=actor, sessionId=session, eventId=event["eventId"])
        for matter in MATTERS:
            records = cleanup("query_owned", table.query, KeyConditionExpression=Key("pk").eq(f"TENANT#{TENANT}#MATTER#{matter}"), ProjectionExpression="pk, sk", Limit=50)
            if records:
                if records.get("LastEvaluatedKey"):
                    result["cleanupErrors"].append("metadata_pagination")
                state["keys"].update((item["pk"], item["sk"]) for item in records["Items"])
        if subjects:
            # Only the exact new Cognito subjects can match. No document bodies
            # or other tenants' records are returned; bound the scan to one page.
            records = cleanup("scoped_grant_lookup", table.scan, FilterExpression=Attr("verifiedSubject").is_in(subjects) & Attr("pk").begins_with("GATEWAY#GRANT#"), ProjectionExpression="pk, sk", Limit=400)
            if records:
                if records.get("LastEvaluatedKey"):
                    result["cleanupErrors"].append("grant_lookup_pagination")
                state["keys"].update((item["pk"], item["sk"]) for item in records["Items"])
        for pk, sk in sorted(state["keys"]):
            cleanup("delete_owned_row", table.delete_item, Key={"pk": pk, "sk": sk})
        for key in sorted(state["objects"]):
            if not key.startswith(f"tenants/{TENANT}/"):
                result["cleanupErrors"].append("unexpected_object_scope")
                continue
            cleanup("delete_owned_object", s3.delete_object, Bucket=BUCKET, Key=key)
        for username in created_users:
            cleanup("delete_owned_user", cognito.admin_delete_user, UserPoolId=POOL, Username=username)
        save()
        print(json.dumps({"result": result["status"], "errorType": result.get("errorType"), "awsErrorCode": result.get("awsErrorCode"), "budget": result["budget"], "cleanupErrors": result["cleanupErrors"]}))
    return 0 if result["status"] == "PASS" and not result["cleanupErrors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
