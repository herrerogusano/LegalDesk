# AgentCore Runtime V2 impact

Assessed **2026-11-03**, `eu-west-1`, repository baseline `ed959af`.
**Recommendation: (a) keep the existing V1 runtime; no migration is required on
November 12. Add a review stop before runtime recreation.** The supplied AWS
Health notice says existing runtimes retain their platform, whereas new runtimes
default to V2 from **2026-11-12**. The date is evidence from the owner-supplied
notice, **not independently retrieved from Health**; the public [platform guide]
still describes V1 as the creation default as of this assessment.

## 1. Current runtime: verified V1

Live `ListAgentRuntimes` returned one runtime. `GetAgentRuntime` using **boto3
1.43.97** returned the following selected fields on 2026-11-03.
Numeric configuration version `9` is distinct from platform `V1`
([GetAgentRuntime API], [platform guide]).

```json
{
  "agentRuntimeId": "harness_LegalDeskPhase01-Kh25KQHkM9",
  "agentRuntimeName": "harness_LegalDeskPhase01",
  "agentRuntimeVersion": "9",
  "status": "READY",
  "platformVersion": "V1",
  "lifecycleConfiguration": {
    "idleRuntimeSessionTimeout": 900,
    "maxLifetime": 28800
  }
}
```

Reproduce without invoking the agent or printing environment values:

```python
import boto3
c = boto3.client("bedrock-agentcore-control", region_name="eu-west-1")
r = c.get_agent_runtime(
    agentRuntimeId="harness_LegalDeskPhase01-Kh25KQHkM9", agentRuntimeVersion="9")
print({k: r.get(k) for k in (
    "agentRuntimeId", "agentRuntimeVersion", "status", "platformVersion",
    "lifecycleConfiguration")})
```

**Client caveat:** installed AWS CLI **2.35.20** omitted `platformVersion` in
its `get-agent-runtime` output; absence there did not establish the platform.
The newer SDK returned the explicit value above. Live `GetHarness` for
`LegalDeskPhase01-7EMjvNs1PC` returned `READY`, `harnessVersion: "9"`, the same
runtime ID, `memory: {"disabled": {}}`, and
`environmentVariables.DISABLE_ADOT_OBSERVABILITY: "true"` (selected API output).

## 2. Harness cannot currently pin a platform in IaC

Live schema command:

```powershell
aws cloudformation describe-type --type RESOURCE --type-name AWS::BedrockAgentCore::Harness --region eu-west-1 --output json --no-cli-pager
```

Parsed `Schema` results: **no occurrence of `platform`**, and
`Environment.AgentCoreRuntimeEnvironment` has only `AgentRuntimeArn`,
`AgentRuntimeName`, `AgentRuntimeId`, `LifecycleConfiguration`,
`NetworkConfiguration`, `FilesystemConfigurations`. The three runtime identity
paths are read-only. Complete `createOnlyProperties` output:

```json
[
  "/properties/HarnessName",
  "/properties/Memory/ManagedMemoryConfiguration/EncryptionKeyArn"
]
```

The [CreateHarness API], [UpdateHarness API] and their nested [runtime environment
request] likewise expose no platform version. Installed boto3 models agree
(inspection only; no create/update calls). **Option (b) is unavailable through
the current Harness resource/API.** Direct Runtime APIs expose `platformVersion`,
but changing the Harness-managed runtime out of band is not an established
supported pin/rollback workflow; **unverified, do not execute**. Obtain AWS
confirmation if preserving V1 across recreation becomes mandatory.

## 3. What could create a new runtime?

Repository search across `infra/`, `scripts/`, `agent/`, `backend/` for
`AWS::BedrockAgentCore::(Harness|Runtime)`, `create_agent_runtime`,
`create-agent-runtime`, `create_harness`, `create-harness`, `platformVersion`,
`platform_version` found one Harness resource in
[`phase-01-harness.yaml`](../infra/cloudformation/phase-01-harness.yaml), plus
the Harness type in the change-set expectations. **No directly declared Runtime
or runtime-creation code was found.** AWS documents that [each Harness has a
managed Runtime][quotas].

- **Harness replacement:** changing either create-only path above; also removing
  and adding the logical resource or creating a separate Harness/stack. These
  can provision a new runtime (schema create handler permits
  `bedrock-agentcore:CreateAgentRuntime`). The supplied notice implies V2 if the
  provider omits the platform after November 12; **the Harness provider's future
  choice is unverified**.
- **Stack delete/recreate or disaster recovery:** recreating
  `legaldesk-phase-01` creates its Harness again (template above); this needs the
  same platform review. A new session/microVM or numeric runtime version alone
  is not a new Runtime resource ([platform guide]).
- **Phase 16:** [draft PR #76], head `55e2693`, contains only the proposed plan
  and ADR. T4 changes Lambda execution-role policies; it does not currently
  prescribe Harness recreation. IAM policy changes and inherited
  `ExecutionRoleArn` changes are not the Harness create-only paths. **No Phase 16
  deployment/change set exists in that PR to prove replacement behavior.**
  Review its eventual change set rather than predict replacement from IAM alone.

The live schema's **update** handler also permits `CreateAgentRuntime`; this
does not prove it creates one on ordinary updates, but a non-replaced Harness
alone cannot prove its managed runtime ID stayed fixed. Compare both IDs before
and after an approved update. Existing treatment of inherited IAM changes and
conditional `Memory` replacement is documented in
[`phase15_changeset_preview.py`](../scripts/phase15_changeset_preview.py) and
the [T9 runbook](phase-15-live-runbook.md), steps 2/4; do not turn a known
non-create-only conditional change into an automatic migration.

## 4. Behavior and code impact

| Area | Evidence and assessment |
|---|---|
| Session, microVM, process state | The [lifecycle guide] specifies one microVM per session, reuse on follow-up calls, idle-timer reset, and termination at idle/max lifetime. Live values are 900/28800 seconds (§1). The [platform guide] retains session isolation and ephemeral state; V2 restores **new instances** from a prepared snapshot. No documented change was found requiring process reset after every invocation. This does **not** prove managed Harness history behavior. |
| Startup state | [V2 optimization] requires fresh request-time IDs, clocks, entropy and renewable credentials; startup state is copied into every restored instance. Startup sockets need reconnecting, startup Gateway catalogs can become stale, and library RNG state needs care. Our managed Harness internals are AWS-owned and **not inspected or validated on V2**. |
| Our state assumptions | [ADR-021 D7](../ARCHITECTURE_DECISIONS.md#d7-memoria) makes the backend the history owner. [`AgentChatWorker._history/_compute`](../backend/src/legaldesk/agent_chat.py) reloads accepted events and passes them to one loop call. [`invoke_agent_chat`](../agent/src/legaldesk_agent/client.py) sends that history, tools and scoped headers on each call, with request-local stream accumulation. It reuses the derived `runtimeSessionId`; memory disabled alone does **not** prove absence of hidden same-session Harness history (ADR-021 pending verification). |
| Authorization and caches | [`bind_harness_invocation`](../backend/src/legaldesk/agent_integration.py) verifies identity/scope and generates UUID/expiry during binding. [`application.py`](../backend/src/legaldesk/application.py) uses DynamoDB stores for sessions, jobs, bindings and grants, plus backend-owned Memory. [`agent_chat_lambda.py`](../backend/src/legaldesk/agent_chat_lambda.py) caches composition/clients in **Lambda**, not in the managed Runtime. No reviewed application correctness dependency on retained Harness-process state was found; managed internals remain unverified. |
| Cold start and deployment | The [AWS announcement] reports about 2 seconds P75 in an echo benchmark, not LegalDesk end-to-end latency or an SLA. Memory reclamation changes resource use; it does not mean discarding live conversation objects. The [platform guide] warns V2 creation/update takes minutes and startup must become healthy within 120 seconds. |
| Limits and streaming | [Quotas] applies Runtime quotas to Harness: 2 vCPU/8 GB per session, 15-minute synchronous request timeout, 100 MB payload, 10 MB stream chunk, 60-minute stream. No V2-specific reduction is documented there. Our client still caps the loop at 90 seconds/3 iterations and the Lambda client read timeout is 120 seconds (client and Lambda files above). Streaming compatibility on V2 is **not live-tested**. |
| Tools/Gateway | Harness [create][CreateHarness API]/[update][UpdateHarness API] still configure tools independently of the platform. No required API/IAM migration was found. Our adapter supplies tools/headers per invocation; the Gateway and target Lambdas are separate resources (Harness template, adapter above). Managed OAuth renewal, tool discovery and stream/result compatibility on V2 remain **unverified**. |
| Logs/metrics | [Runtime observability] still documents native invocation/error/latency/session metrics; V2 cross-account metrics go to the resource owner. No cross-account invocation is evidenced here. Reclaimed memory can change resource-usage readings ([AWS announcement]); do not equate lower memory with lower request volume. ADOT is off (§1); no V2 log-redaction or managed-instrumentation behavior was tested. |

## 5. Action, guard and future test plan

**Keep V1 now (a).** This preserves the verified deployment and avoids an
unneeded migration. **Do not add an unsupported Harness property to pin V1 (b).**
V2 could improve cold starts and memory use, but the [AWS announcement] also
states its memory rate is higher: lower total cost is workload-dependent, not
established for this short-loop workload.

**Runbook guard (documented, not a new code gate):** before an approved Harness
change on/after **2026-11-12**, STOP for Add/Remove, actual replacement, unresolved
`Conditional` replacement, or stack recreation until the supervisor reviews
platform choice. Resolve conditionals against the live create-only schema and
exact changed paths. Capture Harness ID, runtime ID and explicit platform before
and after; STOP if either identity/platform changes unexpectedly. This extends
the T9 review discipline without changing its existing gate implementation.

**Option (c), later and deliberately:** first confirm with AWS how to select and
roll back platform versions for a Harness-managed Runtime. Then obtain separate
cost/change approval and test synthetic same-session follow-ups, a fresh session,
idle termination/re-entry, cold start and update readiness, credential refresh,
Gateway tools, partial streams/timeouts, and cross-matter denial. Check unique
invocation IDs, backend history without duplicate/stale hidden context, scoped
headers, ADOT remaining off, redacted logs and native metrics. Preserve both
resource IDs/platform evidence and a supported rollback procedure before testing.
These live checks are **not executed or authorized by this assessment**.

Validation here: source/schema review and successful list/get/describe calls
only; **zero agent/model invocations, zero AWS mutations, no test infrastructure
or added runtime spend**. No application/IaC code changed; application suites
and deploy/synth were not run for this documentation-only change.

[platform guide]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html
[GetAgentRuntime API]: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GetAgentRuntime.html
[CreateHarness API]: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateHarness.html
[UpdateHarness API]: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateHarness.html
[runtime environment request]: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_HarnessAgentCoreRuntimeEnvironmentRequest.html
[quotas]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
[draft PR #76]: https://github.com/herrerogusano/LegalDesk/pull/76
[lifecycle guide]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html
[V2 optimization]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html
[AWS announcement]: https://aws.amazon.com/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/
[Runtime observability]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html
