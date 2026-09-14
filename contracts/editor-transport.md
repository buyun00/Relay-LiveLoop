# Relay LiveLoop Editor transport contract

The Host and the Unity Editor worker exchange durable JSON files in a configured
directory outside the source workspace. This transport does not infer that an
operation completed from listener liveness, file movement, or elapsed time.

## Request

The Host writes `incoming/<jobId>.request.json` atomically. The UTF-8 JSON object
has exactly these fields:

- `jobId`: stable safe identifier. Reuse binds to the exact request bytes.
- `kind`: neutral Editor provider operation identifier.
- `inputSnapshot`: immutable source/configuration identity for this attempt.
- `providerId`: exact provider expected to execute the request.
- `artifactRoot`: absolute output root contained by the configured artifact root.
- `requestedAtUtc`, `expiresAtUtc`: offset-bearing ISO-8601 timestamps.
- `payloadJson`: operation-specific JSON object encoded as a string.

The SHA-256 of the exact request file is its `requestDigest`. An exact retry in
`incoming`, `processing`, or with a matching durable result starts no new work.
Reusing the same `jobId` with different bytes, snapshot, provider, or digest is
`INPUT_CHANGED`. Expiry rejects only genuinely new work; it does not hide a
previously accepted result.

## Result

The Editor worker publishes `results/<jobId>.result.json` atomically. It contains
`jobId`, `requestDigest`, `inputSnapshot`, `providerId`, `attemptId`, `status`,
`completedAtUtc`, `resultJson`, `error`, and `artifacts`.

- `completed` requires valid `resultJson` and no error.
- `failed` requires a structured error.
- `state_unknown` requires a structured `STATE_UNKNOWN` error.
- Error facts include `code`, `stage`, `message`, `recoverable`,
  `runtimeChangedKnown`, and `runtimeChanged`.
- Artifacts must remain under the configured artifact root and match their
  declared SHA-256 and byte size.

A missing result means only that no durable result is available yet. A Host wait
timeout is recoverable and reports `runtimeChanged=false`; it never republishes
the request or claims that Editor work did not run. Recovery remains the Editor
worker/provider's responsibility and must reconcile its durable attempt record
without replaying unknown side effects.
