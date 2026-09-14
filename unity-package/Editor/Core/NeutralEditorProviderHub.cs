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
                return ImmediateFailure(
                    "INVALID_REQUEST",
                    "editor_provider_dispatch",
                    "Editor job request is missing.",
                    false,
                    true,
                    false);
            }

            switch (execution.Request.kind)
            {
                case "compile":
                    return BeginCompile(execution);
                case "asset.build":
                    return BeginAssetBuild(execution);
                case "source.locate":
                    return BeginSourceLocate(execution);
                case "source.edit":
                    return BeginSourceEdit(execution);
                default:
                    return ImmediateFailure(
                        "CAPABILITY_UNAVAILABLE",
                        "editor_provider_dispatch",
                        "Editor job kind is not supported.",
                        true,
                        true,
                        false);
            }
        }

        public IEditorJobOperation Recover(EditorJobExecution execution)
        {
            if (execution == null || execution.Request == null) return null;
            switch (execution.Request.kind)
            {
                case "compile":
                    return RecoverCompile(execution);
                case "asset.build":
                    return RecoverAssetBuild(execution);
                case "source.locate":
                    return RecoverSourceLocate(execution);
                case "source.edit":
                    return RecoverSourceEdit(execution);
                default:
                    return null;
            }
        }

        private IEditorJobOperation BeginCompile(EditorJobExecution execution)
        {
            CompileJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "compile",
                ValidateCompile,
                out payload,
                out validation)) return validation;
            if (_compile == null) return CapabilityUnavailable("compile");
            return InvokeBegin(
                "COMPILE_FAILED",
                "compile",
                true,
                () => _compile.BeginCompile(payload, execution));
        }

        private IEditorJobOperation BeginAssetBuild(EditorJobExecution execution)
        {
            AssetBuildJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "asset.build",
                ValidateAsset,
                out payload,
                out validation)) return validation;
            if (_asset == null) return CapabilityUnavailable("asset.build");
            return InvokeBegin(
                "RESOURCE_BUILD_FAILED",
                "asset.build",
                true,
                () => _asset.BeginAssetBuild(payload, execution));
        }

        private IEditorJobOperation BeginSourceLocate(EditorJobExecution execution)
        {
            SourceJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "source.locate",
                value => ValidateSource(value, false),
                out payload,
                out validation)) return validation;
            if (_source == null) return CapabilityUnavailable("source.locate");
            return InvokeBegin(
                "INTERNAL_ERROR",
                "source.locate",
                true,
                () => _source.BeginLocate(payload, execution));
        }

        private IEditorJobOperation BeginSourceEdit(EditorJobExecution execution)
        {
            SourceJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "source.edit",
                value => ValidateSource(value, true),
                out payload,
                out validation)) return validation;
            if (_source == null) return CapabilityUnavailable("source.edit");
            return InvokeBegin(
                "STATE_UNKNOWN",
                "source.edit",
                false,
                () => _source.BeginEdit(payload, execution));
        }

        private IEditorJobOperation RecoverCompile(EditorJobExecution execution)
        {
            CompileJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "compile",
                ValidateCompile,
                out payload,
                out validation)) return validation;
            if (_compile == null) return null;
            return InvokeRecovery("compile", () => _compile.RecoverCompile(payload, execution));
        }

        private IEditorJobOperation RecoverAssetBuild(EditorJobExecution execution)
        {
            AssetBuildJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "asset.build",
                ValidateAsset,
                out payload,
                out validation)) return validation;
            if (_asset == null) return null;
            return InvokeRecovery("asset.build", () => _asset.RecoverAssetBuild(payload, execution));
        }

        private IEditorJobOperation RecoverSourceLocate(EditorJobExecution execution)
        {
            SourceJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "source.locate",
                value => ValidateSource(value, false),
                out payload,
                out validation)) return validation;
            if (_source == null) return null;
            return InvokeRecovery("source.locate", () => _source.RecoverLocate(payload, execution));
        }

        private IEditorJobOperation RecoverSourceEdit(EditorJobExecution execution)
        {
            SourceJobPayload payload;
            IEditorJobOperation validation;
            if (!TryParseAndValidate(
                execution.Request.payloadJson,
                "source.edit",
                value => ValidateSource(value, true),
                out payload,
                out validation)) return validation;
            if (_source == null) return null;
            return InvokeRecovery("source.edit", () => _source.RecoverEdit(payload, execution));
        }

        private static IEditorJobOperation InvokeBegin(
            string failureCode,
            string stage,
            bool runtimeChangedKnown,
            Func<IEditorJobOperation> invoke)
        {
            try
            {
                var operation = invoke();
                if (operation != null)
                {
                    return new ClassifiedEditorJobOperation(
                        operation,
                        failureCode,
                        stage,
                        runtimeChangedKnown);
                }
                return ImmediateFailure(
                    failureCode,
                    stage,
                    "Provider returned no operation after it was entered.",
                    false,
                    runtimeChangedKnown,
                    false);
            }
            catch (Exception ex)
            {
                return ImmediateFailure(
                    failureCode,
                    stage,
                    ex.GetType().Name + ": " + ex.Message,
                    false,
                    runtimeChangedKnown,
                    false);
            }
        }

        private static IEditorJobOperation InvokeRecovery(
            string stage,
            Func<IEditorJobOperation> invoke)
        {
            try
            {
                var operation = invoke();
                return operation == null
                    ? null
                    : new ClassifiedEditorJobOperation(
                        operation,
                        "STATE_UNKNOWN",
                        stage,
                        false);
            }
            catch (Exception ex)
            {
                return ImmediateFailure(
                    "STATE_UNKNOWN",
                    stage,
                    ex.GetType().Name + ": " + ex.Message,
                    false,
                    false,
                    false);
            }
        }

        private static bool TryParseAndValidate<T>(
            string json,
            string stage,
            Func<T, string> validate,
            out T payload,
            out IEditorJobOperation failure) where T : class
        {
            payload = null;
            failure = null;
            try
            {
                payload = Parse<T>(json);
                var problem = validate(payload);
                if (problem == null) return true;
                failure = ContractMismatch(stage, problem);
                return false;
            }
            catch (Exception ex)
            {
                failure = ContractMismatch(stage, ex.GetType().Name + ": " + ex.Message);
                return false;
            }
        }

        private static T Parse<T>(string json) where T : class
        {
            if (string.IsNullOrWhiteSpace(json)) throw new ArgumentException("Editor job payload is empty.");
            var value = JsonUtility.FromJson<T>(json);
            if (value == null) throw new ArgumentException("Editor job payload could not be parsed.");
            return value;
        }

        private static string ValidateCompile(CompileJobPayload payload)
        {
            if (string.IsNullOrWhiteSpace(payload.buildTarget) || string.IsNullOrWhiteSpace(payload.configuration) ||
                payload.sourceInputs == null || payload.sourceInputs.Count == 0 || payload.sourceInputs.Count > 4096 ||
                payload.defines == null || payload.defines.Count > 1024 ||
                payload.references == null || payload.references.Count > 4096)
            {
                return "Compile payload shape is incomplete or exceeds bounds.";
            }
            return null;
        }

        private static string ValidateAsset(AssetBuildJobPayload payload)
        {
            if (string.IsNullOrWhiteSpace(payload.packageId) || string.IsNullOrWhiteSpace(payload.buildProfileId) ||
                payload.affectedAssetIds == null || payload.affectedAssetIds.Count == 0 ||
                payload.affectedAssetIds.Count > 10000)
            {
                return "Asset build payload shape is incomplete or exceeds bounds.";
            }
            return null;
        }

        private static string ValidateSource(SourceJobPayload payload, bool edit)
        {
            if (string.IsNullOrWhiteSpace(payload.sourceGuid) || payload.localId < 0 ||
                string.IsNullOrWhiteSpace(payload.propertyPath) ||
                (edit && (payload.expectedValueJson == null || payload.replacementValueJson == null)))
            {
                return "Source payload shape is incomplete.";
            }
            return null;
        }

        private static IEditorJobOperation ContractMismatch(string stage, string message)
        {
            return ImmediateFailure(
                "CONTRACT_MISMATCH",
                stage,
                message,
                false,
                true,
                false);
        }

        private static IEditorJobOperation CapabilityUnavailable(string stage)
        {
            return ImmediateFailure(
                "CAPABILITY_UNAVAILABLE",
                stage,
                "The required project adapter is not registered.",
                true,
                true,
                false);
        }

        private static IEditorJobOperation ImmediateFailure(
            string code,
            string stage,
            string message,
            bool recoverable,
            bool runtimeChangedKnown,
            bool runtimeChanged)
        {
            return new ImmediateEditorJobOperation(EditorJobPollResult.Failed(
                new EditorJobError
                {
                    code = code,
                    stage = stage,
                    message = message,
                    recoverable = recoverable,
                    runtimeChangedKnown = runtimeChangedKnown,
                    runtimeChanged = runtimeChanged
                }));
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

        private sealed class ClassifiedEditorJobOperation : IEditorJobOperation
        {
            private readonly IEditorJobOperation _inner;
            private readonly string _failureCode;
            private readonly string _stage;
            private readonly bool _runtimeChangedKnown;

            public ClassifiedEditorJobOperation(
                IEditorJobOperation inner,
                string failureCode,
                string stage,
                bool runtimeChangedKnown)
            {
                _inner = inner;
                _failureCode = failureCode;
                _stage = stage;
                _runtimeChangedKnown = runtimeChangedKnown;
            }

            public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
            {
                try
                {
                    var result = _inner.Poll(mainThreadBudget);
                    if (result != null) return result;
                    return Failed("Provider returned no poll result after it was entered.");
                }
                catch (Exception ex)
                {
                    return Failed(ex.GetType().Name + ": " + ex.Message);
                }
            }

            private EditorJobPollResult Failed(string message)
            {
                return EditorJobPollResult.Failed(new EditorJobError
                {
                    code = _failureCode,
                    stage = _stage,
                    message = message,
                    recoverable = false,
                    runtimeChangedKnown = _runtimeChangedKnown,
                    runtimeChanged = false
                });
            }
        }
    }
}
#endif
