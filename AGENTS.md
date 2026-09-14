# Relay LiveLoop repository guidance

This repository contains only generic Relay LiveLoop tooling, contracts, a generic Unity package, and synthetic tests.

- Keep protocol behavior shared across HTTP, CLI, and MCP adapters. These adapters call one command service.
- Keep Unity runtime code free of project-specific types and SDK bindings.
- Use synthetic identifiers and paths in tests and examples.
- Do not commit generated runtime artifacts, credentials, machine configuration, application binaries, screenshots, recordings, or third-party SDK binaries.
- Preserve explicit result facts. Accepted work is not automatically completed or verified.
- Stop scripts must preserve attached application processes unless the caller explicitly opts in to stopping one.
- Run the relevant tests and the local publication check before pushing changes.

