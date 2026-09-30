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
