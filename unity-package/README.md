# Relay LiveLoop Unity package

This package contains generic Unity Runtime and Editor coordination primitives. It does not include project adapters, commercial SDK integrations, credentials, or game content.

Runtime control types compile only for the Unity Editor or Development builds. Production Player builds do not export the control surface.

Project-specific providers must register explicitly and retain the package's session, generation, main-thread, artifact, and evidence boundaries.
