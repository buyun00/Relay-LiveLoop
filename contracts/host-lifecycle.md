# Host lifecycle contract

Relay LiveLoop exposes authenticated Host lifecycle facts for local deployment scripts. Host shutdown never implies Player shutdown.

`GET /status` includes `hostLifecycle`:

| Field | Meaning |
| --- | --- |
| `state` | `running` or `draining`. Draining rejects new work while existing jobs finish. |
| `acceptingCommands` | Whether commands that can start new work are accepted. Read-only status/report calls remain available while draining. |
| `shutdownRequested` | Whether an authenticated graceful shutdown has been accepted. |
| `shutdownRequestId` / `requestedAtUtc` | Correlation and time for the accepted shutdown request, otherwise null. |
| `waitForActiveJobs` | Accepted request policy, otherwise null. |
| `safeToExit` | True only after an authenticated drain was accepted and the durable ledger has no queued or running jobs. Do not derive this from listener health or total job counts. |
| `playerPolicy` | Always `preserve` for this endpoint. |
| `activeJobs` | Exact queued/running jobs with ID, operation, state, stage, interruptibility, cancellation, runtime-change, and update-time facts. |
| `nonInterruptibleJobs` | The active-job subset in `runtime_apply` or `runtime_reconcile`; these stages must not be terminated or replayed. |

`GET /capabilities` includes `hostLifecycle` with the endpoint, authentication, supported mode, wait support, and Player policy.

`POST /lifecycle/shutdown` accepts exactly:

```json
{
  "protocolVersion": 1,
  "requestId": "request_synthetic_shutdown",
  "mode": "graceful",
  "preservePlayer": true,
  "waitForActiveJobs": true
}
```

The endpoint requires the same local bearer authentication as every other HTTP route. With `waitForActiveJobs=false`, any active job returns `CONFLICT` and leaves the Host running. With `waitForActiveJobs=true`, the Host enters `draining`, rejects new work, lets existing jobs reach terminal durable states, and exits only after `safeToExit=true`. There is no force mode and no option on this endpoint to stop an attached Player.

The CLI equivalent is `relay-liveloop shutdown --wait-for-active-jobs --json`. A response of `accepted` means the drain request was accepted; scripts should observe the returned lifecycle fields and then wait for the Host process or endpoint to exit. It is not evidence that a Player exited.
