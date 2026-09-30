# Relay LiveLoop source-save provider contract

`source.locate` and `source.edit` use the same command service as HTTP, CLI and MCP. Source saving does not apply a Player update. A request accepted into an Editor mailbox cannot claim that its asset was saved.

## Host seam

Construct `EditorSourceProvider(ledger, artifacts, transport, resolver, workspace_root, provider_id)` and pass it to `build_command_service(..., source_provider=provider)`. A private resolver must expose `is_verified=True` and `source_save_contract_version=1` only after the matching transactional Editor provider is registered. Missing or older providers remain unavailable. The default command-line server has no project source mapping and does not invent one.

`SourceBindingResolver.resolve(task, source_id, context) -> dict` resolves current authenticated runtime provenance. `validate(task, binding) -> None` checks the same current launch, task target, page and owner generation again before dispatch and receipt consumption. It must refuse ambiguous, stale, foreign or unavailable provenance. File names or same-name runtime objects are insufficient evidence.

The binding contains exactly `taskId`, `taskUpdatedAt`, `sessionId`, `launchId`, `runtimeRevision`, `targetId`, `pageId`, `ownerGeneration`, `sourceId`, `sourceGuid`, `localId`, `assetPath`, `sourceHash`, and `properties`. The asset path is relative to the configured workspace under `Assets/`; supported extensions are `.prefab`, `.asset`, and `.mat`. Scene saving is excluded. The Host verifies the GUID against the adjacent meta file, checks the persistent asset SHA-256 and refuses reparse traversal. `properties` is an explicit allowlist. Each transaction edits one asset and refuses duplicate property edits.

Source commands require `context.sessionId`, `context.expectedLaunchId`, and `context.expectedRuntimeRevision`. A source edit still uses the existing protocol arguments `expectedSourceHash` and `edits[{sourceId,property,expectedValue,newValue}]`. The Editor envelope stores the full immutable context and input digest before dispatch; polling uses `job.status`. Restart reconciliation never creates a replacement for a lost dispatched job. After constructing the service, call `service.providers.recover_pending()` before serving requests. The update coordinator owns only prepare/iterate recovery.

## Editor seam

`SourceJobPayload` retains its existing fields and adds optional `string providerContextJson`. Parse it as `SourceEditContext`: schema `relay.liveloop.source-edit-context`, version `1`, canonical `bindingJson`, `expectedSourceHash`, and the complete `edits` list. Each edit contains `propertyPath`, `expectedValueJson`, and `replacementValueJson`. The outer legacy fields repeat only the first edit; a transactional provider must use the complete context.

Implement `ISourceEditBackend` for one explicitly resolved asset:

```csharp
void ValidateBinding(SourceJobPayload payload, SourceEditContext context);
bool IsTargetClean { get; }
string ReadPersistedHash();
string ReadProperty(string propertyPath);
string ReadPersistedProperty(string propertyPath);
void WriteProperty(string propertyPath, string canonicalValueJson);
string SaveTarget(); // returns the exact persistent SHA-256 after this save
```

The backend must prove GUID/local-id/property/type binding, preserve unrelated unsaved changes, and operate only on its one clean target asset. Load isolated Prefab contents or a targeted asset as appropriate; never save scenes or call global `SaveAssets`. Persistent readback must reflect serialized disk content, independently of an in-memory modified object. Release temporary asset state through `IDisposable` when needed.

Return `DurableSourceEdit.CreateOperation(payload, context, execution, backend, recovering)` from BeginEdit/RecoverEdit. The operation runs on the Editor worker's main thread, journals before mutation/save, saves one target, verifies persistent values/hash, and emits one sealed `source_save_receipt` artifact. On recovery it reads a matching terminal journal and validates current persistent state. Missing, nonterminal, contradictory or stale journals return `STATE_UNKNOWN` with no write replay.

Failed saves restore only attributable state. A failure before disk change restores in-memory values without rewriting an unchanged file. A known completed file write may be rolled back only while its hash still matches this operation's returned save hash. A save that wrote and then threw without returning its hash stays unknown and does not overwrite potentially changed disk content. Rollback success requires the exact original disk hash and persistent values.

## Result facts

The receipt binds job, attempt, request digest, full provenance and before/after hashes. `phase` is `completed`, `failed`, or `state_unknown`; `sourcePersisted`, `sourceChangedKnown`, `sourceChanged`, `runtimeApplied`, `rollbackAttempted`, and `rollbackSucceeded` are explicit facts. Source operations always have `runtimeApplied=false`. An unknown receipt cannot assert saved or source-change facts.

The Host compares the result JSON with the sealed receipt artifact and verifies the persistent file hash before writing `sourceSaved=true`. Accepted responses keep terminal facts unknown even if the task already has an older saved fact. Build output, runtime apply, input and fresh-frame verification remain separate commands and evidence. A saved asset must still be prepared by the actual resource builder, applied once to the bound Player, and verified through new input and a newer matching frame.

A new saved-source fact atomically clears previous runtime/check/review/fresh facts to unknown while retaining their historical evidence. Source saving cannot carry those acceptance facts forward or assert new ones. The next actual apply and verification commands must establish them again for the saved inputs.

A private launch wrapper may set `args.source_provider_factory(context)` before calling the normal `_serve(args)` composition. The context contains the actual `ledger`, `artifacts`, `editor_transport`, and `runtime_transport` objects; the returned provider is passed to the same `build_command_service`. Provider construction never changes verified facts. `RelayLiveLoopEditorWorkerBootstrap.ConfiguredJobRoot` exposes the actual active worker root, or null when stopped, so private registration receipts cannot advertise another mailbox.


## Saved-source resource preparation

A source-edit job also records immutable `taskScope` fields `sessionId`, `target`, `reference`, `goal`, `allowedImpact`, and `acceptance`. Saved facts may advance task timestamps, while later preparation must retain the exact original authorized scope. `Ledger.latest_task_job(task_id, operation)` returns the latest attempt including pending or unknown attempts; an older completed save cannot hide a newer uncertain operation.

A private launch wrapper may additionally set `args.preparation_provider_factory(context)`. The normal server invokes it after source-provider construction and before `build_command_service`. Its context contains the actual `ledger`, `artifacts`, `source_provider`, `preparation_provider`, `runtime_provider`, `editor_transport`, and `runtime_transport`. A decorator must preserve the existing provider identity, current input snapshot and authenticated runtime ownership. HTTP, CLI and MCP continue to use that same service.

A preparation provider may return optional `resourceBuildEvidence`, exactly the registered artifact metadata fields `artifactId`, `kind`, `sha256`, `size`, and `mediaType`, with kind `source_resource_build_receipt`. This requires `verify_resource_build_evidence(task, metadata, plan) -> dict`. The coordinator calls that verifier before publishing the plan and again immediately before runtime dispatch. A missing verifier refuses the plan. The verifier must bind the registered bytes to the original durable preparation request, the latest completed source-save receipt, the current persistent asset hash, actual emitted manifest/bundle bytes, and the exact task/session/launch/page generation. Registering caller-supplied JSON is insufficient.

The artifact must distinguish the parent plan `inputSnapshot` from the actual resource request `resourceInputSnapshot`. Its source hash must equal the completed save receipt `afterHash`. Its manifest and bundle hashes describe actual produced files; produced evidence cannot assert activated or runtime-applied facts. Explicit dependency coverage remains explicit; the contract makes no claim that an entire dependency graph was frozen.

A page resource route may leave `expectedRuntimeRevisionAfter` null only when it provides complete `targetGenerationsAfter`; the Player assigns the revision and actual authenticated readback must establish the transition. This does not register a route or make an unsupported runtime capable. Source-bound resource preparation must use the actual existing supported runtime route and stay within the authorized one-page impact.

Durable child build envelopes are recorded before dispatch. Recovery polls/reconciles the original request and never enqueues a replacement for pending, missing, expired or unknown results. A source or explicit-input drift prevents incorrect apply. Failed build or apply retains the earlier completed source-save fact and its receipt. Approval-required results retain preparation journals and artifact identity, so subsequent review does not destroy recovery evidence.


## Loaded source versus a known saved delta

A resolver may expose `runtime_evidence(task, binding) -> dict` with exactly `runtimeSourceHash`, `persistedSourceHash`, `runtimeMatched`, `knownTaskSave`, and `sourceSaveJobId`. The loaded hash must originate from the actual source bytes sampled into the loaded build content. A runtime getter cannot replace that hash with the current source-file hash. The persistent hash is independently read from disk. Missing loaded evidence cannot establish a source-bound resource build.

`source.locate` includes those extra evidence fields in its returned `result.source`; the resolver binding and Editor `bindingJson` retain their original exact fields. A known completed save may explain the hash difference only when its original durable job, exact registered receipt, authorized task scope, source GUID/local ID/path/property scope, session/launch/runtime revision and page owner generation all match. Its `afterHash` must equal current disk; the actual loaded hash must equal the loaded hash recorded when that save began (or its first save `beforeHash`). Unknown, foreign and externally drifted states are refused. Source-bound build still requires the latest save to be completed. During a subsequent pending save, identity readback may reuse only the prior completed receipt explicitly recorded by that pending job while disk still equals the prior known saved hash; it cannot claim the pending save completed or explain a new unreceipted write.

A known save returns the new persistent `sourceHash` and the old `runtimeSourceHash`, with `runtimeMatched=false`; the locate result can retain a false task runtime fact. Matching source hashes in a fresh authenticated source readback do not by themselves assert the entire task runtime acceptance fact. Build and actual apply evidence remain required. A new edit records its independently read `runtimeSourceEvidence` outside the original binding JSON. This also supports multiple known same-task saves before the first resource activation.

Source owner generation comes from authenticated source provenance. It must match the completed save and current live provenance; it is not inferred to equal a global view counter. The coordinator separately retains its actual observed target generations throughout prepare/apply.
