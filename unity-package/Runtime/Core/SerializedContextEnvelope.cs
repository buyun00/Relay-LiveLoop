#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;

namespace RelayLiveLoop
{
    // Context crosses replaceable module generations only as versioned plain bytes. This type
    // deliberately exposes no Unity object, runtime Type, delegate, Task, cancellation token,
    // socket, or authentication token field.
    public sealed class SerializedContextEnvelope
    {
        public SerializedContextEnvelope(
            string schemaId,
            int schemaVersion,
            string mediaType,
            string sourceSessionId,
            long sourceModuleGeneration,
            long sourceViewGeneration,
            string dataRevision,
            byte[] payload,
            int maximumPayloadBytes = 64 * 1024)
        {
            SchemaId = RuntimeSessionIdentity.RequireCanonicalValue(schemaId, nameof(schemaId));
            if (schemaVersion <= 0) throw new ArgumentOutOfRangeException(nameof(schemaVersion));
            MediaType = RuntimeSessionIdentity.RequireCanonicalValue(mediaType, nameof(mediaType));
            SourceSessionId = RuntimeSessionIdentity.RequireCanonicalValue(sourceSessionId, nameof(sourceSessionId));
            if (sourceModuleGeneration < 0) throw new ArgumentOutOfRangeException(nameof(sourceModuleGeneration));
            if (sourceViewGeneration < 0) throw new ArgumentOutOfRangeException(nameof(sourceViewGeneration));
            DataRevision = RuntimeSessionIdentity.RequireCanonicalValue(dataRevision, nameof(dataRevision));
            if (maximumPayloadBytes <= 0) throw new ArgumentOutOfRangeException(nameof(maximumPayloadBytes));
            if (payload == null || payload.Length > maximumPayloadBytes)
            {
                throw new ArgumentException("Context payload is missing or exceeds the configured limit.", nameof(payload));
            }

            SchemaVersion = schemaVersion;
            SourceModuleGeneration = sourceModuleGeneration;
            SourceViewGeneration = sourceViewGeneration;
            Payload = (byte[])payload.Clone();
        }

        public string SchemaId { get; private set; }
        public int SchemaVersion { get; private set; }
        public string MediaType { get; private set; }
        public string SourceSessionId { get; private set; }
        public long SourceModuleGeneration { get; private set; }
        public long SourceViewGeneration { get; private set; }
        public string DataRevision { get; private set; }
        public byte[] Payload { get; private set; }
    }
}
#endif
