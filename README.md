# Relay LiveLoop

Relay LiveLoop is a local development tool for applying prepared code and asset changes to a running Unity development player through explicit, auditable providers.

The repository is organized around one command service:

- `host/` owns tasks, plans, jobs, artifacts, and update state.
- `api/`, `clients/`, `mcp/`, and `relay_liveloop.py` expose thin protocol adapters.
- `unity-package/` provides generic runtime and Editor integration points.
- `contracts/` defines the shared envelopes, operation catalog, and provider boundaries.
- `scripts/` contains local bootstrap, lifecycle, diagnostic, portability, and publication checks.

The protocol keeps source changes, runtime application, automated checks, visual review, and clean-start verification as separate facts. A successful request never fills in evidence that was not actually collected.

This branch is under active development. Runtime capabilities remain unavailable until a concrete provider reports current evidence for them.

