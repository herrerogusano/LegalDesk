# Phase 14 document-security runbook

This runbook applies to the authenticated fictional/public beta only. It does
not authorize real legal data or an AWS deployment.

## Normal path

1. The backend issues a server-owned key and metadata record in
   `PENDING_UPLOAD`; public PUTs target `quarantine/`, outside the Bedrock
   source prefix `tenants/`.
2. Upload confirmation validates the object `HEAD`, size, media type, and
   tenant/matter/document metadata but does not make it indexable in public
   mode.
3. The exact GuardDuty Malware Protection result is corroborated by a fresh
   `HEAD` and the `GuardDutyMalwareScanStatus` object tag. Only
   `NO_THREATS_FOUND` copies the object server-side to its canonical `tenants/`
   key, permits the canonical sidecar and `UPLOADED` transition, then deletes
   the quarantine object.
   Event parsing requires `scanStatus` (`COMPLETED`, `SKIPPED`, or `FAILED`)
   and the matching `scanResultDetails.scanResultStatus`; status reasons are
   bounded and never logged verbatim.

## Fail-closed and incident handling

Unknown envelope fields, wrong account/region/bucket/key, scope mismatch,
missing or mismatched tags, changed ETag/version, malformed metadata, and
provider read errors do not promote an object. Operators should retain only
bounded event ID/status/scope metadata in audit records; never log tokens,
document bodies, or full provider responses.

`THREATS_FOUND`, `UNSUPPORTED`, `ACCESS_DENIED`, and `FAILED` delete the
original and sidecar when cleanup succeeds and persist `FAILED`. The metadata
transition is conditional on `PENDING_UPLOAD + PENDING`; a reconciler or
concurrent event therefore wins or loses explicitly rather than being
overwritten by an unconditional save. Cleanup or metadata persistence failure
is an incident: metadata remains non-indexable, the object may remain for
bounded retry/quarantine, and no client retry may start ingestion. Duplicate
terminal events retry cleanup. A later clean event cannot resurrect a failed
document.

If copy, sidecar, conditional metadata transition, or quarantine deletion
fails, the record remains `PENDING_UPLOAD` or `FAILED` and ingestion remains
blocked; retry/reconciliation may safely repeat the exact operation. A threat
event arriving after a completed clean transition is treated as a conditional
conflict: canonical/quarantine objects are removed fail-closed and the
metadata conflict is surfaced for operator reconciliation rather than allowing
an unsafe overwrite.

## Deployment gate

The remaining deployment work must configure an exact EventBridge rule,
GuardDuty/S3 permissions, account and region filters, and alerting before
public mode is enabled. The application role needs narrowly scoped S3
`GetObject`/`HeadObject`/`GetObjectTagging`/`DeleteObject` on `quarantine/`,
and `CopyObject`/sidecar writes plus reads on the canonical `tenants/` prefix;
the data source role must not read `quarantine/`. IaC, quarantine retention,
and AWS rollout require the
separate Phase 14 deployment/cost approval; this local tranche creates no
resources and performs no AWS calls.
