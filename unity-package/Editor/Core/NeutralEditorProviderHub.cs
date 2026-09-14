#if UNITY_EDITOR
using System;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class NeutralEditorProviderHub : IEditorJobProvider
    {
        private enum ProviderSlot
        {
            Compile,
            Asset,
            Source
        }

        private ICompileJobProvider _compile;
        private IAssetBuildJobProvider _asset;
        private ISourceJobProvider _source;

        public NeutralEditorProviderHub(string providerId)
        {
            if (string.IsNullOrWhiteSpace(providerId)) throw new ArgumentException("Provider id is required.", nameof(providerId));
            ProviderId = providerId;
        }

        public string ProviderId { get; private set; }

        public IDisposable RegisterCompile(ICompileJobProvider provider)
        {
            if (provider == null) throw new ArgumentNullException(nameof(provider));
            if (_compile != null) throw new InvalidOperationException("Compile provider is already registered.");
            _compile = provider;
            return new Registration(this, ProviderSlot.Compile, provider);
        }

        public IDisposable RegisterAsset(IAssetBuildJobProvider provider)
        {
            if (provider == null) throw new ArgumentNullException(nameof(provider));
            if (_asset != null) throw new InvalidOperationException("Asset build provider is already registered.");
            _asset = provider;
            return new Registration(this, ProviderSlot.Asset, provider);
        }

        public IDisposable RegisterSource(ISourceJobProvider provider)
        {
            if (provider == null) throw new ArgumentNullException(nameof(provider));
            if (_source != null) throw new InvalidOperationException("Source provider is already registered.");
            _source = provider;
            return new Registration(this, ProviderSlot.Source, provider);
        }

        public bool Supports(string kind)
        {
            return string.Equals(kind, "compile", StringComparison.Ordinal) ||
                string.Equals(kind, "asset.build", StringComparison.Ordinal) ||
                string.Equals(kind, "source.locate", StringComparison.Ordinal) ||
                string.Equals(kind, "source.edit", StringComparison.Ordinal);
        }

        public IEditorJobOperation Begin(EditorJobExecution execution)
        {
            if (execution == null || execution.Request == null)
            {
                return ImmediateFailure("INVALID_REQUEST", "editor_provider_dispatch", "Editor job request is missing.", false);
            }

            try
            {
                switch (execution.Request.kind)
                {
                    case "compile":
                        if (_compile == null) return CapabilityUnavailable("compile");
                        var compilePayload = Parse<CompileJobPayload>(execution.Request.payloadJson);
                        var compileError = ValidateCompile(compilePayload);
                        return compileError ?? _compile.BeginCompile(compilePayload, execution) ??
                            ImmediateFailure("CONTRACT_MISMATCH", "compile", "Compile provider returned no operation.", false);
                    case "asset.build":
                        if (_asset == null) return CapabilityUnavailable("asset.build");
                        var assetPayload = Parse<AssetBuildJobPayload>(execution.Request.payloadJson);
                        var assetError = ValidateAsset(assetPayload);
                        return assetError ?? _asset.BeginAssetBuild(assetPayload, execution) ??
                            ImmediateFailure("CONTRACT_MISMATCH", "asset.build", "Asset provider returned no operation.", false);
                    case "source.locate":
                        if (_source == null) return CapabilityUnavailable("source.locate");
                        var locatePayload = Parse<SourceJobPayload>(execution.Request.payloadJson);
                        var locateError = ValidateSource(locatePayload, false);
                        return locateError ?? _source.BeginLocate(locatePayload, execution) ??
                            ImmediateFailure("CONTRACT_MISMATCH", "source.locate", "Source provider returned no operation.", false);
                    case "source.edit":
                        if (_source == null) return CapabilityUnavailable("source.edit");
                        var editPayload = Parse<SourceJobPayload>(execution.Request.payloadJson);
                        var editError = ValidateSource(editPayload, true);
                        return editError ?? _source.BeginEdit(editPayload, execution) ??
                            ImmediateFailure("CONTRACT_MISMATCH", "source.edit", "Source provider returned no operation.", false);
                    default:
                        return ImmediateFailure("CAPABILITY_UNAVAILABLE", "editor_provider_dispatch", "Editor job kind is not supported.", true);
                }
            }
            catch (Exception ex)
            {
                return ImmediateFailure("CONTRACT_MISMATCH", "editor_provider_dispatch", ex.GetType().Name + ": " + ex.Message, false);
            }
        }

        public IEditorJobOperation Recover(EditorJobExecution execution)
        {
            if (execution == null || execution.Request == null) return null;
            try
            {
                switch (execution.Request.kind)
                {
                    case "compile":
                        return _compile == null
                            ? null
                            : _compile.RecoverCompile(Parse<CompileJobPayload>(execution.Request.payloadJson), execution);
                    case "asset.build":
                        return _asset == null
                            ? null
                            : _asset.RecoverAssetBuild(Parse<AssetBuildJobPayload>(execution.Request.payloadJson), execution);
                    case "source.locate":
                        return _source == null
                            ? null
                            : _source.RecoverLocate(Parse<SourceJobPayload>(execution.Request.payloadJson), execution);
                    case "source.edit":
                        return _source == null
                            ? null
                            : _source.RecoverEdit(Parse<SourceJobPayload>(execution.Request.payloadJson), execution);
                    default:
                        return null;
                }
            }
            catch
            {
                // Recovery must not turn an unreadable or ambiguous prior attempt into a new run.
                return null;
            }
        }

        private static T Parse<T>(string json) where T : class
        {
            if (string.IsNullOrWhiteSpace(json)) throw new ArgumentException("Editor job payload is empty.");
            var value = JsonUtility.FromJson<T>(json);
            if (value == null) throw new ArgumentException("Editor job payload could not be parsed.");
            return value;
        }

        private static IEditorJobOperation ValidateCompile(CompileJobPayload payload)
        {
            if (string.IsNullOrWhiteSpace(payload.buildTarget) || string.IsNullOrWhiteSpace(payload.configuration) ||
                payload.sourceInputs == null || payload.sourceInputs.Count == 0 || payload.sourceInputs.Count > 4096 ||
                payload.defines == null || payload.defines.Count > 1024 ||
                payload.references == null || payload.references.Count > 4096)
            {
                return ImmediateFailure("INVALID_REQUEST", "compile", "Compile payload is incomplete or exceeds bounds.", false);
            }
            return null;
        }

        private static IEditorJobOperation ValidateAsset(AssetBuildJobPayload payload)
        {
            if (string.IsNullOrWhiteSpace(payload.packageId) || string.IsNullOrWhiteSpace(payload.buildProfileId) ||
                payload.affectedAssetIds == null || payload.affectedAssetIds.Count == 0 ||
                payload.affectedAssetIds.Count > 10000)
            {
                return ImmediateFailure("INVALID_REQUEST", "asset.build", "Asset build payload is incomplete or exceeds bounds.", false);
            }
            return null;
        }

        private static IEditorJobOperation ValidateSource(SourceJobPayload payload, bool edit)
        {
            if (string.IsNullOrWhiteSpace(payload.sourceGuid) || payload.localId < 0 ||
                string.IsNullOrWhiteSpace(payload.propertyPath) ||
                (edit && (payload.expectedValueJson == null || payload.replacementValueJson == null)))
            {
                return ImmediateFailure("INVALID_REQUEST", edit ? "source.edit" : "source.locate", "Source payload is incomplete.", false);
            }
            return null;
        }

        private static IEditorJobOperation CapabilityUnavailable(string stage)
        {
            return ImmediateFailure(
                "CAPABILITY_UNAVAILABLE",
                stage,
                "The required project adapter is not registered.",
                true);
        }

        private static IEditorJobOperation ImmediateFailure(
            string code,
            string stage,
            string message,
            bool recoverable)
        {
            return new ImmediateEditorJobOperation(EditorJobPollResult.Failed(
                AtomicEditorJobStore.Error(code, stage, message, recoverable)));
        }

        private void Unregister(ProviderSlot slot, object expected)
        {
            switch (slot)
            {
                case ProviderSlot.Compile:
                    if (ReferenceEquals(_compile, expected)) _compile = null;
                    break;
                case ProviderSlot.Asset:
                    if (ReferenceEquals(_asset, expected)) _asset = null;
                    break;
                case ProviderSlot.Source:
                    if (ReferenceEquals(_source, expected)) _source = null;
                    break;
            }
        }

        private sealed class Registration : IDisposable
        {
            private NeutralEditorProviderHub _hub;
            private readonly ProviderSlot _slot;
            private readonly object _provider;

            public Registration(NeutralEditorProviderHub hub, ProviderSlot slot, object provider)
            {
                _hub = hub;
                _slot = slot;
                _provider = provider;
            }

            public void Dispose()
            {
                var hub = _hub;
                if (hub == null) return;
                hub.Unregister(_slot, _provider);
                _hub = null;
            }
        }

        private sealed class ImmediateEditorJobOperation : IEditorJobOperation
        {
            private readonly EditorJobPollResult _result;

            public ImmediateEditorJobOperation(EditorJobPollResult result)
            {
                _result = result;
            }

            public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
            {
                return _result;
            }
        }
    }
}
#endif
