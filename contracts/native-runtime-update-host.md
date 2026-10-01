# Native runtime update Host contract

Status: isolated v006 Host implementation candidate, based on frozen v005. Synthetic and static checks do not establish real Unity, Player, native, device, or production acceptance. The private adapter and production repositories are outside this candidate and were not modified.

## Scope and composition

The public v1 command operations remain unchanged. Callers use the existing task, prepare, iterate, status, and artifact flows. Native runtime calls stay behind the authenticated runtime `invoke` boundary; they are not added to `execute`, `validate_command`, HTTP, CLI, or MCP operation registries.

The public v1 command operations remain unchanged. Callers do not supply a route or runtime manifest to choose the path. Profile-backed code analysis classifies each compiler result as `BODY_ONLY`, `STRUCTURE`, or `UNCHANGED`; a resource provider must expose read-only `analyze_changes(task)` evidence bound to its current input snapshot and profile digest. Missing, malformed, unverified, or `UNKNOWN` classification fails closed.

| Code analysis | Resource analysis | Automatic selection |
| --- | --- | --- |
| `BODY_ONLY`, one-assembly Hotfix shape verified | `UNCHANGED` | `HOTFIX`, gated by session-bound verified Hotfix capability evidence |
| `STRUCTURE` | `UNCHANGED` | `MODULE_RELOAD` |
| `UNCHANGED` | `CHANGED` | `RESOURCE_ONLY` |
| `BODY_ONLY` | `CHANGED` | `HOTFIX_AND_ASSET_RELOAD` |
| `STRUCTURE` | `CHANGED` | `MODULE_AND_ASSET_RELOAD` |

`BODY_ONLY` with an unsupported shape or missing Hotfix capability is refused; it is not silently routed to module reload. When the resource analysis is `UNCHANGED`, the composite wrapper delegates the code-only input/profile boundary and does not build or bind a resource candidate. A resource-only delta is classified without generating a code-reload candidate. The batch contains no executable `RESOURCE_ONLY` or `HOTFIX_AND_ASSET_RELOAD` handler, so those routes require a V-owned runtime capability and fail before resource build/apply when absent. The current Host transport does not implement the new session-bound Hotfix capability response either; Hotfix plans are not created without it.

`MODULE_AND_ASSET_RELOAD` remains an optional route enabled only when a `CompositePreparationProvider` is explicitly constructed with both a code provider and a resource provider. The normal `_serve` composition does not configure a resource provider. The included resource analyzer/provider is synthetic; this batch contains no private/live resource adapter or activation handler.

The composite immutable plan binds task/session/input/profile/runtime state, code manifest and payload closure, resource manifest and archive closure, context requirements, affected views/modules, resource-release transition, and before/after generations. One coordinator apply invokes the runtime provider once; it is not one atomic Player transaction. Its separately journaled mutation order is `module.quiesce`, `module.dispose`, `module.load`, `resource.activate`, `module.restore`. There is no rollback. Each dispatched stage records its identity, before/after observation, and acknowledgement state. A known partial transition retains the exact acknowledged steps; lost or contradictory attribution is `STATE_UNKNOWN` with `runtimeChanged: null`, forbids replay, and requires a fresh session.

The code-only `MODULE_RELOAD` path preserves the current resource release. It does not imply a paired resource update. For structural code plus changed resources, automatic-selection evidence and both input/profile bindings are retained with the common immutable plan; apply still uses the frozen sequence above.

## Runtime contract and safety

The task descriptor binds a manifest artifact and the exact artifact closure. The manifest fixes task/session/launch/input identity, route, scope/impact, module closure, before-generations, after-generations, and payload artifact identities. `inputSha256` is the lowercase SHA-256 of compact UTF-8 JSON with ordinal key order and every nested `inputSha256` property omitted; only JSON strings, integers, booleans, nulls, arrays, and objects are accepted.

For module reload, payload and private-frame validation completes before quiesce/dispose. The preflight requires the prepared active module identity/generation and `nativeReady: true`. Module load/restore and resource activation results must echo the expected module identity/generation; resource activation also binds the exact prepared release, archive hash, and manifest hash. `module.reconcile` is diagnostic only. No possibly-dispatched mutation is replayed. Runtime transport identity/revision attribution is discarded on an unknown result.

These response-field expectations are cross-end contract dependencies, not evidence of a live Player run. Host-side artifact hashing proves the dispatched bytes match the immutable plan; it does not independently prove which assembly or resources a real Player loaded.

## Compiler-input coverage and evidence

The Native source-compile profile digest binds the normalized server-owned profile, including Unity installation/version, build target/group, development flag, subtarget, scripting defines, project/build settings, declarations, module/dependency closure, assembly/baseline pins, and timeout. Server profile document version 2 retains compile context version 3 and input receipt version 1, including its unchanged `/2` profile digest and exact field sets. An explicitly configured version 3 profile uses context version 4, receipt version 2, and a `/3` profile digest. Compile analysis remains version 3 in both cases.

The Editor receipt enumerates observed assembly sources/references and compiler-option paths, project/package/assembly configuration, HybridCLR package files, pinned Unity/Mono/compiler files, bundled Roslyn and BuildPipeline files, output hashes, compile settings/result, and before/after digests for the listed inputs. Host independently revalidates that recorded inventory and its required configuration/toolchain files at receipt sealing, recovery, plan finalization, and immediately before apply. This verifies the enumerated set; it is not proof that the set contains every file actually read by the compiler.

Every valid receipt must carry all three limitation markers:

- `ACTUAL_COMPILER_PROCESS_IDENTITY_NOT_PROVEN`
- `ADDITIONAL_COMPILER_ARGUMENTS_MAY_REFERENCE_UNENUMERATED_FILES_NOT_PROVEN`
- `ANALYZER_OR_SOURCE_GENERATOR_TRANSITIVE_FILE_READS_NOT_PROVEN`

Preparation evidence labels coverage `ENUMERATED_ONLY_NOT_PROVEN_COMPLETE` and preserves the exact receipt limitation list. Host compares that list with the sealed receipt during plan validation/recovery and revalidation before apply. The same label, exact limitations, and receipt artifact ID/hash are copied into the terminal apply job result. A missing marker or changed evidence fails closed. No compiler PID requirement is added, and no arbitrary argument/analyzer transitive-read closure is claimed.

`inputSnapshot` is a separate digest of configured build target/configuration/defines/references and declared source-input paths/bytes. Neither it nor the profile digest proves that declarations cover all compiler-consumed inputs.

## Explicit server-owned external compiler inputs

The existing server profile registry can opt into profile document version 3. Each profile then requires one additional field, `trustedSourceRoots`. A root has exactly `rootId`, `path`, and `inputs`: a safe stable identity, its exact canonical existing absolute directory, and a non-empty closed list of canonical relative files. This is server configuration loaded by the existing normal provider/factory. HTTP, CLI and MCP command arguments cannot install roots or expand that file list.

```json
{
  "trustedSourceRoots": [
    {
      "rootId": "shared-sources",
      "path": "C:/SyntheticSources",
      "inputs": ["Runtime/Core/Extension.cs", "Runtime/Core/Support.cs"]
    }
  ]
}
```

This example shows the additional field only, not a complete profile. Root IDs match `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`, are unique ignoring case, and cannot be `project` or `unity`. There are 1..32 roots and at most 100000 configured external files. Volume roots, overlapping/nested roots, aliases, reparse-backed roots/files, external hard-link aliases, missing/non-regular files, and duplicate relative identities are refused. Membership uses path segments and verified canonical paths, never a string-prefix directory whitelist. Roots are sorted by rootId using .NET Ordinal order; each inputs list is likewise sorted. Context version 4 and receipt version 2 must echo this normalized array exactly. A legacy context/receipt must omit the additional field entirely, including null or empty values.

The `/3` digest binds root identities, canonical directories and the closed input lists in addition to every prior profile field. A receipt input still has exactly `scope`, `path`, `roles`, `sha256`, and `size`. An external scope is `source:` followed by the configured rootId; its path is relative to that root and must match the configured file list exactly. All configured external inputs must be present. The existing project/unity scopes remain unchanged. Absolute assembly graph and compiler-option paths retain their real canonical paths, with recorded roles derived from that actual graph. Receipt version 2 refuses missing or additional input paths/roles against the independently reconstructed graph, project-configuration and Unity-toolchain inventories, including an otherwise hash-valid unconfigured external file.

The length-prefixed input-set digest algorithm remains unchanged: the new scope identity participates in the same before/after SHA256. Every actual file SHA256/size is re-read after compile and during Host receipt validation, recovery and before apply. Neither a configured root nor a matching profile digest waives byte drift, output closure, task/job/session/context identity or version checks. An old receipt cannot satisfy a new-root profile by omitting its roots. Profile version 2 remains a strict two-root mode and rejects the new configuration field.

The declared project `sourceInputs` and their `inputSnapshot` keep their existing separate semantics. Explicit external-input handling does not claim complete arbitrary compiler argument/analyzer/source-generator reads or a proven compiler process identity. The same enumerated-only coverage classification and required limitations remain mandatory.

## Completed Reload baseline and compiler call timing

A normal source compiler Reload can retain the authenticated `module.load` assembly closure, DLL/PDB hashes, MVID, module generation, compiler receipt and registered runtime manifest. The retained version is usable by the next normal Prepare only after the same iterate job is completed with `runtimeChanged: true` and `runtimeMatched: true`. Accepted, running, failed, unknown, changed-launch or mismatched-generation results cannot advance the baseline. Each Prepare reopens the immutable artifacts and checks their identities and bytes. It derives new task-owned Library baseline copies without overwriting configured baselines or changing the server profile pointer. After Host restart, a fresh authenticated runtime observation is required.

Input receipt versions 1 and 2 remain readable without a per-call timing claim. Receipt version 3 (legacy roots) or 4 (explicit trusted roots) adds exactly two `compilerInvocations`: `actualCompileDll` and `normalTypeDbCompile`. Each carries its API, UTC interval, independent monotonic `elapsedTicks`/`frequency`, exact task-owned output directory and complete output hashes. The first interval surrounds the actual synchronous SDK `CompileDll` call, which returns void. A separate normal `CompilePlayerScripts` call supplies the genuine TypeDB and returned assembly set. Both DLL inventories must match the sealed payload output hashes; the normal invocation supplies payload symbols. Recovery validates the existing inventories without invoking either compiler again. Unsupported settings or changed outputs fail closed.

The independent calls add real Prepare latency. Baseline compilation, dispatch, hashing and the TypeDB call are not included in the measured `actualCompileDll` interval. Synthetic command-service and receipt tests verify these Host boundaries; they do not establish actual Unity compiler timing, Reload, Hotfix or Player acceptance.

## Lost Editor result and no-replay boundary

Before placing an Editor request in the incoming mailbox, Host creates a durable per-job submission tombstone bound to the job ID, request digest, input snapshot, and provider ID. A missing incoming/processing request and result cannot be interpreted as an unsubmitted job after that tombstone exists; same-job recovery returns `STATE_UNKNOWN` and does not write a replacement request.

The neutral Editor worker also persists a per-job attempt tombstone before returning a new compile attempt. If a request and attempt have been archived but the result has disappeared, a duplicate same-job request is rejected as unknown. It cannot start another `BeginCompile` for that job. The regression exercises the file-backed mailbox, processing/archive transition, result deletion, and duplicate recovery in a temporary directory. This is synthetic worker evidence, not a real Unity compile.

## Verification

Run the pinned-baseline-plus-overlay checks with:

```powershell
.\verification\verify-in-scratch.ps1 -UnityRoot 'C:\Program Files\Unity 2022.3.62f3'
```

The Host suite uses synthetic Editor receipts and a fake authenticated Player transport. The C# checks compile isolated fixtures with the Unity-bundled C# compiler; they do not launch Unity Editor or invoke `CompileDllCommand`. No actual HybridCLR compile, compiler-process identity proof, complete arbitrary-input closure, Player/native runtime, device/product acceptance, or publication is performed.
