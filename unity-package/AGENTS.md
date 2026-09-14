# Unity package guidance

- Keep Runtime code independent of project types, Editor assemblies, and commercial SDKs.
- Keep Editor-only APIs under `Editor/` and reference the Runtime assembly through its asmdef.
- Preserve the `UNITY_EDITOR || DEVELOPMENT_BUILD` release boundary for Runtime control types.
- Use stable committed `.meta` GUIDs for package files and directories.
- Treat missing providers as unavailable. Do not add fallback success responses or infer review evidence.
- Validate with the synthetic harness and Unity 2022.3 managed reference assemblies before publication.
