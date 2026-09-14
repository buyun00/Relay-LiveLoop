#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;

namespace RelayLiveLoop
{
    public sealed class RuntimeProviderHub
    {
        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly ProviderRegistry _providers;
        private readonly RuntimeRevisionClock _runtimeRevisions;

        public RuntimeProviderHub(IRelayLiveLoopMainThreadGuard mainThread, ProviderRegistry providers)
            : this(mainThread, providers, null)
        {
        }

        public RuntimeProviderHub(
            IRelayLiveLoopMainThreadGuard mainThread,
            ProviderRegistry providers,
            RuntimeRevisionClock runtimeRevisions)
        {
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
            _providers = providers ?? throw new ArgumentNullException(nameof(providers));
            _runtimeRevisions = runtimeRevisions;
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> ObservePage(
            ProviderAddress address,
            PageOperationRequest request)
        {
            return InvokePage(address, "page.observe", provider => provider.Observe(request));
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RefreshPage(
            ProviderAddress address,
            PageOperationRequest request)
        {
            return InvokePage(address, "page.refresh", provider => provider.Refresh(request));
        }

        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CapturePageContext(
            ProviderAddress address,
            PageOperationRequest request)
        {
            return InvokePage(address, "page.capture_context", provider => provider.CaptureContext(request));
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RestorePageContext(
            ProviderAddress address,
            PageOperationRequest request,
            SerializedContextEnvelope context)
        {
            if (context == null) return MissingContext<ProviderReply<PageRuntimeState>>("page.restore_context");
            return InvokePage(address, "page.restore_context", provider => provider.RestoreContext(request, context));
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RebuildPage(
            ProviderAddress address,
            PageOperationRequest request)
        {
            return InvokePage(address, "page.rebuild", provider => provider.Rebuild(request));
        }

        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> ObserveModule(
            ProviderAddress address,
            ModuleOperationRequest request)
        {
            return InvokeModule(address, "module.observe", provider => provider.Observe(request));
        }

        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureModuleContext(
            ProviderAddress address,
            ModuleOperationRequest request)
        {
            return InvokeModule(address, "module.capture_context", provider => provider.CaptureContext(request));
        }

        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> QuiesceModule(
            ProviderAddress address,
            ModuleOperationRequest request)
        {
            return InvokeModule(address, "module.quiesce", provider => provider.Quiesce(request));
        }

        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> DisposeModule(
            ProviderAddress address,
            ModuleOperationRequest request)
        {
            return InvokeModule(address, "module.dispose", provider => provider.Dispose(request));
        }

        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> LoadModule(
            ProviderAddress address,
            ModuleOperationRequest request)
        {
            return InvokeModule(address, "module.load", provider => provider.Load(request));
        }

        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> RestoreModule(
            ProviderAddress address,
            ModuleOperationRequest request,
            SerializedContextEnvelope context)
        {
            if (context == null) return MissingContext<ProviderReply<ModuleRuntimeState>>("module.restore");
            return InvokeModule(address, "module.restore", provider => provider.Restore(request, context));
        }

        // Call only after the previous provider invocation has returned to this stable layer.
        // This purges all stable-host references before an adapter attempts module unload.
        public long AdvanceProviderOwnerGeneration(string ownerId)
        {
            _mainThread.AssertMainThread();
            return _providers.AdvanceOwnerGeneration(ownerId);
        }

        private RelayLiveLoopResult<T> InvokePage<T>(
            ProviderAddress address,
            string stage,
            Func<IPageProvider, RelayLiveLoopResult<T>> invoke)
        {
            _mainThread.AssertMainThread();
            var addressError = ValidateAddress(address, RelayLiveLoopProviderKind.Page, stage);
            if (addressError != null) return RelayLiveLoopResult<T>.Failure(addressError);
            var resolved = _providers.Resolve<IPageProvider>(
                RelayLiveLoopProviderKind.Page,
                address.ProviderId,
                address.OwnerId,
                address.OwnerGeneration);
            if (!resolved.Succeeded) return RelayLiveLoopResult<T>.Failure(resolved.Error);
            return InvokeResolved(stage, resolved.Value, invoke);
        }

        private RelayLiveLoopResult<T> InvokeModule<T>(
            ProviderAddress address,
            string stage,
            Func<IModuleProvider, RelayLiveLoopResult<T>> invoke)
        {
            _mainThread.AssertMainThread();
            var addressError = ValidateAddress(address, RelayLiveLoopProviderKind.Module, stage);
            if (addressError != null) return RelayLiveLoopResult<T>.Failure(addressError);
            var resolved = _providers.Resolve<IModuleProvider>(
                RelayLiveLoopProviderKind.Module,
                address.ProviderId,
                address.OwnerId,
                address.OwnerGeneration);
            if (!resolved.Succeeded) return RelayLiveLoopResult<T>.Failure(resolved.Error);
            return InvokeResolved(stage, resolved.Value, invoke);
        }

        private RelayLiveLoopResult<T> InvokeResolved<TProvider, T>(
            string stage,
            TProvider provider,
            Func<TProvider, RelayLiveLoopResult<T>> invoke) where TProvider : class
        {
            var revisionBefore = _runtimeRevisions == null
                ? null
                : _runtimeRevisions.CurrentRevision;
            RelayLiveLoopResult<T> result;
            try
            {
                result = invoke(provider);
            }
            catch (Exception ex)
            {
                return ProviderException<T>(stage, revisionBefore, ex);
            }

            if (result == null)
            {
                return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.ContractMismatch,
                    stage,
                    "Provider returned no result.",
                    false,
                    null));
            }

            return ValidateRuntimeTransition(stage, revisionBefore, result);
        }

        private RelayLiveLoopResult<T> ProviderException<T>(
            string stage,
            string revisionBefore,
            Exception exception)
        {
            var revisionAfter = _runtimeRevisions == null
                ? null
                : _runtimeRevisions.CurrentRevision;
            if (revisionBefore != null &&
                !string.Equals(revisionBefore, revisionAfter, StringComparison.Ordinal))
            {
                return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.StateUnknown,
                    stage,
                    "Provider threw after publishing a runtime revision transition: " +
                        exception.GetType().Name + ": " + exception.Message,
                    false,
                    true));
            }

            return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                RelayLiveLoopErrorCode.InternalError,
                stage,
                exception.GetType().Name + ": " + exception.Message,
                false,
                null));
        }

        private RelayLiveLoopResult<T> ValidateRuntimeTransition<T>(
            string stage,
            string revisionBefore,
            RelayLiveLoopResult<T> result)
        {
            if (_runtimeRevisions == null)
            {
                var unboundReport = result.Succeeded
                    ? (object)result.Value as IProviderRuntimeChangeReport
                    : null;
                if (unboundReport != null && unboundReport.RuntimeChanged == true)
                {
                    return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        stage,
                        "A changed provider reply requires a RuntimeProviderHub bound to the authoritative revision clock.",
                        false,
                        false));
                }

                return result;
            }

            var revisionAfter = _runtimeRevisions.CurrentRevision;
            var clockChanged = !string.Equals(revisionBefore, revisionAfter, StringComparison.Ordinal);
            if (!result.Succeeded)
            {
                if (!clockChanged) return result;
                return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.StateUnknown,
                    stage,
                    "Provider returned failure after the authoritative runtime revision changed.",
                    false,
                    true));
            }

            var report = (object)result.Value as IProviderRuntimeChangeReport;
            if (report == null)
            {
                return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                    clockChanged ? RelayLiveLoopErrorCode.StateUnknown : RelayLiveLoopErrorCode.ContractMismatch,
                    stage,
                    "Provider result does not expose runtime transition truth.",
                    false,
                    clockChanged));
            }

            if (!clockChanged)
            {
                if (report.RuntimeChanged == true || report.Transition != null ||
                    report.RuntimeRevisionAfter != null)
                {
                    return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        stage,
                        "Provider claimed a runtime change without advancing the authoritative clock during this call.",
                        false,
                        false));
                }

                return result;
            }

            var transition = report.Transition;
            if (report.RuntimeChanged == true && transition != null &&
                ReferenceEquals(transition.Authority, _runtimeRevisions) &&
                string.Equals(transition.PreviousRevision, revisionBefore, StringComparison.Ordinal) &&
                string.Equals(transition.RuntimeRevisionAfter, revisionAfter, StringComparison.Ordinal) &&
                string.Equals(report.RuntimeRevisionAfter, revisionAfter, StringComparison.Ordinal))
            {
                return result;
            }

            return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                RelayLiveLoopErrorCode.StateUnknown,
                stage,
                "Authoritative runtime revision changed without a matching transition receipt in the provider result.",
                false,
                true));
        }

        private static RelayLiveLoopError ValidateAddress(
            ProviderAddress address,
            RelayLiveLoopProviderKind expectedKind,
            string stage)
        {
            if (address == null || string.IsNullOrWhiteSpace(address.ProviderId) ||
                string.IsNullOrWhiteSpace(address.OwnerId) || address.OwnerGeneration < 0)
            {
                return RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.InvalidMessage,
                    stage,
                    "Provider address is incomplete.",
                    false);
            }
            if (address.Kind != expectedKind)
            {
                return RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.ContractMismatch,
                    stage,
                    "Provider kind does not match the requested operation.",
                    false);
            }
            return null;
        }

        private static RelayLiveLoopResult<T> MissingContext<T>(string stage)
        {
            return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                RelayLiveLoopErrorCode.ContractMismatch,
                stage,
                "Serialized context is required.",
                false));
        }
    }
}
#endif
