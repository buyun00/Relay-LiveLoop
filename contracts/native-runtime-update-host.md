# Native runtime update Host contract

Status: public Host source integration from isolated candidate v005. Static and synthetic checks do not establish real Unity, Player, native, device, or production acceptance.

## Scope and composition

The public v1 command operations remain unchanged. Callers use the existing task, prepare, iterate, status, and artifact flows. Native runtime calls stay behind the authenticated runtime `invoke` boundary; they are not added to `execute`, `validate_command`, HTTP, CLI, or MCP operation registries.

The ordinary prepared routes remain `HOTFIX` and `MODULE_RELOAD`. `MODULE_AND_ASSET_RELOAD` is optional and enabled only when a `CompositePreparationProvider` is explicitly constructed with both a code provider and a resource provider. The default `create_native_update_providers` composition leaves `resource_provider` unset, so this route is not enabled by default. Tests inject a synthetic resource provider; no private/live resource activation handler is included.

The composite immutable plan binds task/session/input/profile/runtime state, code manifest and payload closure, resource manifest and archive closure, context requirements, affected views/modules, resource-release transition, and before/after generations. One coordinator apply invokes the runtime provider once; it is not one atomic Player transaction. Its separately journaled mutation order is `module.quiesce`, `module.dispose`, `module.load`, `resource.activate`, `module.restore`. There is no rollback. Each dispatched stage records its identity, before/after observation, and acknowledgement state. A known partial transition retains the exact acknowledged steps; lost or contradictory attribution is `STATE_UNKNOWN` with `runtimeChanged: null`, forbids replay, and requires a fresh session.

The code-only `MODULE_RELOAD` path preserves the current resource release. It does not imply a paired resource update.

## Runtime contract and safety

The task descriptor binds a manifest artifact and the exact artifact closure. The manifest fixes task/session/launch/input identity, route, scope/impact, module closure, before-generations, after-generations, and payload artifact identities. `inputSha256` is the lowercase SHA-256 of compact UTF-8 JSON with ordinal key order and every nested `inputSha256` property omitted; only JSON strings, integers, booleans, nulls, arrays, and objects are accepted.

For module reload, payload and private-frame validation completes before quiesce/dispose. The preflight requires the prepared active module identity/generation and `nativeReady: true`. Module load/restore and resource activation results must echo the expected module identity/generation; resource activation also binds the exact prepared release, archive hash, and manifest hash. `module.reconcile` is diagnostic only. No possibly-dispatched mutation is replayed. Runtime transport identity/revision attribution is discarded on an unknown result.

These response-field expectations are cross-end contract dependencies, not evidence of a live Player run. Host-side artifact hashing proves the dispatched bytes match the immutable plan; it does not independently prove which assembly or resources a real Player loaded.

## Compiler-input coverage and evidence

The Native source-compile profile digest binds the normalized server-owned profile, including Unity installation/version, build target/group, development flag, subtarget, scripting defines, project/build settings, declarations, module/dependency closure, assembly/baseline pins, and timeout. Editor compile-context and analysis are version 3. The receipt schema remains `relay.liveloop.native-compile-input-receipt` version 1.

The Editor receipt enumerates observed assembly sources/references and compiler-option paths, project/package/assembly configuration, HybridCLR package files, pinned Unity/Mono/compiler files, bundled Roslyn and BuildPipeline files, output hashes, compile settings/result, and before/after digests for the listed inputs. Host independently revalidates that recorded inventory and its required configuration/toolchain files at receipt sealing, recovery, plan finalization, and immediately before apply. This verifies the enumerated set; it is not proof that the set contains every file actually read by the compiler.

Every valid receipt must carry all three limitation markers:

- `ACTUAL_COMPILER_PROCESS_IDENTITY_NOT_PROVEN`
- `ADDITIONAL_COMPILER_ARGUMENTS_MAY_REFERENCE_UNENUMERATED_FILES_NOT_PROVEN`
- `ANALYZER_OR_SOURCE_GENERATOR_TRANSITIVE_FILE_READS_NOT_PROVEN`

Preparation evidence labels coverage `ENUMERATED_ONLY_NOT_PROVEN_COMPLETE` and preserves the exact receipt limitation list. Host compares that list with the sealed receipt during plan validation/recovery and revalidation before apply. The same label, exact limitations, and receipt artifact ID/hash are copied into the terminal apply job result. A missing marker or changed evidence fails closed. No compiler PID requirement is added, and no arbitrary argument/analyzer transitive-read closure is claimed.

`inputSnapshot` is a separate digest of configured build target/configuration/defines/references and declared source-input paths/bytes. Neither it nor the profile digest proves that declarations cover all compiler-consumed inputs.

## Lost Editor result and no-replay boundary

Before placing an Editor request in the incoming mailbox, Host creates a durable per-job submission tombstone bound to the job ID, request digest, input snapshot, and provider ID. A missing incoming/processing request and result cannot be interpreted as an unsubmitted job after that tombstone exists; same-job recovery returns `STATE_UNKNOWN` and does not write a replacement request.

The neutral Editor worker also persists a per-job attempt tombstone before returning a new compile attempt. If a request and attempt have been archived but the result has disappeared, a duplicate same-job request is rejected as unknown. It cannot start another `BeginCompile` for that job. The regression exercises the file-backed mailbox, processing/archive transition, result deletion, and duplicate recovery in a temporary directory. This is synthetic worker evidence, not a real Unity compile.

## Verification

Run the Host regression suites and synthetic C# fixtures from the repository root with:

```powershell
py -3 -B -m unittest tests.test_native_compile_preparation tests.test_native_runtime_wiring
$unityRoot = '<Unity 2022.3 Editor installation root>'
.\tests\unity-synthetic\native-unknown\run-synthetic.ps1 -UnityRoot $unityRoot
```

The Host suite uses synthetic Editor receipts and a fake authenticated Player transport. The C# checks compile isolated fixtures with the Unity-bundled C# compiler; they do not launch Unity Editor or invoke `CompileDllCommand`. These checks do not establish an actual HybridCLR compile, compiler-process identity, complete arbitrary-input closure, Player/native runtime, device/product acceptance, or production deployment.
