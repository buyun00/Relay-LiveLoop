#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;

namespace RelayLiveLoop
{
    public sealed class RuntimeProviderHub
    {
        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly ProviderRegistry _providers;

        public RuntimeProviderHub(IRelayLiveLoopMainThreadGuard mainThread, ProviderRegistry providers)
        {
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
            _providers = providers ?? throw new ArgumentNullException(nameof(providers));
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

        private static RelayLiveLoopResult<T> InvokeResolved<TProvider, T>(
            string stage,
            TProvider provider,
            Func<TProvider, RelayLiveLoopResult<T>> invoke) where TProvider : class
        {
            try
            {
                var result = invoke(provider);
                if (result == null)
                {
                    return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        stage,
                        "Provider returned no result.",
                        false,
                        null));
                }
                return result;
            }
            catch (Exception ex)
            {
                return RelayLiveLoopResult<T>.Failure(RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.InternalError,
                    stage,
                    ex.GetType().Name + ": " + ex.Message,
                    false,
                    null));
            }
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
