# Relay LiveLoop protocol contracts

Protocol version 1 uses one command envelope and one result envelope across HTTP, CLI, and MCP.

- CLI operations keep their dotted names, such as `task.open`.
- MCP tool names use the `relay_liveloop_` prefix and replace dots with underscores.
- Long-running operations return `accepted` with a `jobId`.
- Repeated `requestId` values are idempotent and must not repeat a destructive effect.
- Unknown facts stay `null`.
- Artifact payloads are referenced by identifier; large binary data is not embedded in command responses.

`operations.json` is the machine-readable operation catalog. `provider-contracts.md` describes the dependency direction between the command service and generic Unity providers.

All examples in this directory are synthetic and do not identify an application or workspace.

