#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    public enum RelayLiveLoopProviderKind
    {
        Page,
        Module,
        Hotfix,
        Resource,
        Observation,
        Evidence,
        Context
    }

    public sealed class ProviderRegistration : IDisposable
    {
        private ProviderRegistry _registry;
        private readonly string _key;
        private readonly long _registrationId;

        internal ProviderRegistration(ProviderRegistry registry, string key, long registrationId)
        {
            _registry = registry;
            _key = key;
            _registrationId = registrationId;
        }

        public void Dispose()
        {
            var registry = _registry;
            if (registry == null) return;
            registry.Remove(_key, _registrationId);
            _registry = null;
        }
    }

    public sealed class ProviderRegistry
    {
        private sealed class Entry
        {
            public long RegistrationId;
            public string OwnerId;
            public long OwnerGeneration;
            public object Provider;
        }

        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly Dictionary<string, Entry> _entries =
            new Dictionary<string, Entry>(StringComparer.Ordinal);
        private readonly Dictionary<string, long> _ownerGenerations =
            new Dictionary<string, long>(StringComparer.Ordinal);
        private long _nextRegistrationId;

        public ProviderRegistry(IRelayLiveLoopMainThreadGuard mainThread)
        {
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
        }

        public long GetOwnerGeneration(string ownerId)
        {
            _mainThread.AssertMainThread();
            ValidateIdentifier(ownerId, nameof(ownerId));
            long generation;
            return _ownerGenerations.TryGetValue(ownerId, out generation) ? generation : 0;
        }

        public long AdvanceOwnerGeneration(string ownerId)
        {
            _mainThread.AssertMainThread();
            ValidateIdentifier(ownerId, nameof(ownerId));
            var current = GetOwnerGeneration(ownerId);
            PurgeOwnerGeneration(ownerId, current);
            var next = checked(current + 1);
            _ownerGenerations[ownerId] = next;
            return next;
        }

        public ProviderRegistration Register<T>(
            RelayLiveLoopProviderKind kind,
            string providerId,
            string ownerId,
            long ownerGeneration,
            T provider) where T : class
        {
            _mainThread.AssertMainThread();
            ValidateIdentifier(providerId, nameof(providerId));
            ValidateIdentifier(ownerId, nameof(ownerId));
            if (provider == null) throw new ArgumentNullException(nameof(provider));
            var currentGeneration = GetOwnerGeneration(ownerId);
            if (ownerGeneration != currentGeneration)
            {
                throw new InvalidOperationException("Provider registration uses a stale owner generation.");
            }

            var key = BuildKey(kind, providerId);
            if (_entries.ContainsKey(key))
            {
                throw new InvalidOperationException("A provider with the same kind and id is already registered.");
            }

            var registrationId = checked(++_nextRegistrationId);
            _entries.Add(key, new Entry
            {
                RegistrationId = registrationId,
                OwnerId = ownerId,
                OwnerGeneration = ownerGeneration,
                Provider = provider
            });
            return new ProviderRegistration(this, key, registrationId);
        }

        public RelayLiveLoopResult<T> Resolve<T>(
            RelayLiveLoopProviderKind kind,
            string providerId,
            string expectedOwnerId,
            long expectedOwnerGeneration) where T : class
        {
            _mainThread.AssertMainThread();
            ValidateIdentifier(providerId, nameof(providerId));
            ValidateIdentifier(expectedOwnerId, nameof(expectedOwnerId));
            if (GetOwnerGeneration(expectedOwnerId) != expectedOwnerGeneration)
            {
                return RelayLiveLoopResult<T>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StaleTarget,
                        "resolve_provider",
                        "The requested provider generation is obsolete.",
                        true));
            }

            Entry entry;
            if (!_entries.TryGetValue(BuildKey(kind, providerId), out entry))
            {
                return RelayLiveLoopResult<T>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.CapabilityUnavailable,
                        "resolve_provider",
                        "The requested provider is not registered.",
                        true));
            }

            if (!string.Equals(entry.OwnerId, expectedOwnerId, StringComparison.Ordinal) ||
                entry.OwnerGeneration != expectedOwnerGeneration)
            {
                return RelayLiveLoopResult<T>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StaleTarget,
                        "resolve_provider",
                        "The requested provider belongs to an obsolete module generation.",
                        true));
            }

            var typed = entry.Provider as T;
            if (typed == null)
            {
                return RelayLiveLoopResult<T>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        "resolve_provider",
                        "The registered provider does not implement the requested stable contract.",
                        false));
            }

            return RelayLiveLoopResult<T>.Success(typed);
        }

        public int PurgeOwnerGeneration(string ownerId, long ownerGeneration)
        {
            _mainThread.AssertMainThread();
            ValidateIdentifier(ownerId, nameof(ownerId));
            var keys = new List<string>();
            foreach (var pair in _entries)
            {
                if (string.Equals(pair.Value.OwnerId, ownerId, StringComparison.Ordinal) &&
                    pair.Value.OwnerGeneration == ownerGeneration)
                {
                    keys.Add(pair.Key);
                }
            }

            foreach (var key in keys)
            {
                // Dropping the entry releases the stable host's strong reference. We intentionally
                // do not call back into the old provider while an unload is being finalized.
                _entries[key].Provider = null;
                _entries.Remove(key);
            }

            return keys.Count;
        }

        public void Clear()
        {
            _mainThread.AssertMainThread();
            foreach (var entry in _entries.Values) entry.Provider = null;
            _entries.Clear();
            _ownerGenerations.Clear();
        }

        internal void Remove(string key, long registrationId)
        {
            _mainThread.AssertMainThread();
            Entry entry;
            if (_entries.TryGetValue(key, out entry) && entry.RegistrationId == registrationId)
            {
                entry.Provider = null;
                _entries.Remove(key);
            }
        }

        private static string BuildKey(RelayLiveLoopProviderKind kind, string providerId)
        {
            return ((int)kind).ToString() + ":" + providerId;
        }

        private static void ValidateIdentifier(string value, string parameterName)
        {
            RuntimeSessionIdentity.RequireCanonicalValue(value, parameterName);
        }
    }
}
#endif
