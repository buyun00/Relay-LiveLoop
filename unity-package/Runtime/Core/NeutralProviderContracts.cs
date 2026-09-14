#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    public sealed class ProviderAddress
    {
        public RelayLiveLoopProviderKind Kind { get; set; }
        public string ProviderId { get; set; }
        public string OwnerId { get; set; }
        public long OwnerGeneration { get; set; }
    }

    public sealed class ProviderEvidence
    {
        public string Kind { get; set; }
        public string Stage { get; set; }
        public string ArtifactId { get; set; }
        public string Sha256 { get; set; }
        public string Detail { get; set; }
    }

    public sealed class ProviderReply<T>
    {
        private readonly ProviderEvidence[] _evidence;

        public ProviderReply(T value, bool? runtimeChanged, IReadOnlyList<ProviderEvidence> evidence)
        {
            Value = value;
            RuntimeChanged = runtimeChanged;
            if (evidence == null)
            {
                _evidence = Array.Empty<ProviderEvidence>();
            }
            else
            {
                _evidence = new ProviderEvidence[evidence.Count];
                for (var index = 0; index < evidence.Count; index++) _evidence[index] = evidence[index];
            }
        }

        public T Value { get; private set; }
        public bool? RuntimeChanged { get; private set; }
        public IReadOnlyList<ProviderEvidence> Evidence { get { return _evidence; } }
    }

    public sealed class NeutralPayload
    {
        private readonly byte[] _bytes;

        public NeutralPayload(string schemaId, int schemaVersion, string mediaType, byte[] bytes)
        {
            SchemaId = RuntimeSessionIdentity.RequireCanonicalValue(schemaId, nameof(schemaId));
            if (schemaVersion <= 0) throw new ArgumentOutOfRangeException(nameof(schemaVersion));
            MediaType = RuntimeSessionIdentity.RequireCanonicalValue(mediaType, nameof(mediaType));
            if (bytes == null) throw new ArgumentNullException(nameof(bytes));
            SchemaVersion = schemaVersion;
            _bytes = (byte[])bytes.Clone();
        }

        public string SchemaId { get; private set; }
        public int SchemaVersion { get; private set; }
        public string MediaType { get; private set; }
        public byte[] Bytes { get { return (byte[])_bytes.Clone(); } }
    }

    public sealed class PageOperationRequest
    {
        public string TaskId { get; set; }
        public string PageId { get; set; }
        public long ExpectedViewGeneration { get; set; }
        public NeutralPayload Arguments { get; set; }
    }

    public sealed class PageRuntimeState
    {
        public string PageId { get; set; }
        public string ModuleId { get; set; }
        public long ModuleGeneration { get; set; }
        public long ViewGeneration { get; set; }
        public bool Stable { get; set; }
        public long Frame { get; set; }
        public NeutralPayload State { get; set; }
    }

    public sealed class ModuleOperationRequest
    {
        public string TaskId { get; set; }
        public string ModuleId { get; set; }
        public long ExpectedModuleGeneration { get; set; }
        public IReadOnlyList<string> DependencyClosure { get; set; }
        public NeutralPayload Arguments { get; set; }
    }

    public sealed class ModuleRuntimeState
    {
        public string ModuleId { get; set; }
        public long ModuleGeneration { get; set; }
        public string Stage { get; set; }
        public NeutralPayload State { get; set; }
    }

    public interface IPageProvider
    {
        RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Observe(PageOperationRequest request);
        RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Refresh(PageOperationRequest request);
        RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(PageOperationRequest request);
        RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RestoreContext(PageOperationRequest request, SerializedContextEnvelope context);
        RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Rebuild(PageOperationRequest request);
    }

    public interface IModuleProvider
    {
        RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Observe(ModuleOperationRequest request);
        RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(ModuleOperationRequest request);
        RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Quiesce(ModuleOperationRequest request);
        RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Dispose(ModuleOperationRequest request);
        RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Load(ModuleOperationRequest request);
        RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Restore(ModuleOperationRequest request, SerializedContextEnvelope context);
    }
}
#endif
