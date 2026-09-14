#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class RuntimeObjectHandle
    {
        public RuntimeObjectHandle(string sessionId, string scopeId, long generation, string handleId)
        {
            SessionId = sessionId;
            ScopeId = scopeId;
            Generation = generation;
            HandleId = handleId;
        }

        public string SessionId { get; private set; }
        public string ScopeId { get; private set; }
        public long Generation { get; private set; }
        public string HandleId { get; private set; }
    }

    public sealed class ObjectHandleRegistry
    {
        private sealed class Entry
        {
            public string ScopeId;
            public long Generation;
            public WeakReference Target;
        }

        private readonly RuntimeSessionIdentity _session;
        private readonly IRelayLiveLoopMainThreadGuard _mainThread;
        private readonly Dictionary<string, Entry> _entries =
            new Dictionary<string, Entry>(StringComparer.Ordinal);
        private readonly Dictionary<string, long> _scopeGenerations =
            new Dictionary<string, long>(StringComparer.Ordinal);

        public ObjectHandleRegistry(RuntimeSessionIdentity session, IRelayLiveLoopMainThreadGuard mainThread)
        {
            _session = session ?? throw new ArgumentNullException(nameof(session));
            _mainThread = mainThread ?? throw new ArgumentNullException(nameof(mainThread));
        }

        public long GetGeneration(string scopeId)
        {
            _mainThread.AssertMainThread();
            ValidateScopeId(scopeId);
            long generation;
            return _scopeGenerations.TryGetValue(scopeId, out generation) ? generation : 0;
        }

        public long AdvanceGeneration(string scopeId)
        {
            _mainThread.AssertMainThread();
            ValidateScopeId(scopeId);
            var next = checked(GetGeneration(scopeId) + 1);
            _scopeGenerations[scopeId] = next;
            RemoveScopeEntries(scopeId);
            return next;
        }

        public RuntimeObjectHandle Register(string scopeId, UnityEngine.Object target)
        {
            _mainThread.AssertMainThread();
            ValidateScopeId(scopeId);
            if (target == null) throw new ArgumentNullException(nameof(target));
            var handleId = Guid.NewGuid().ToString("N");
            var generation = GetGeneration(scopeId);
            _entries.Add(handleId, new Entry
            {
                ScopeId = scopeId,
                Generation = generation,
                Target = new WeakReference(target)
            });
            return new RuntimeObjectHandle(_session.SessionId, scopeId, generation, handleId);
        }

        public RelayLiveLoopResult<T> Resolve<T>(RuntimeObjectHandle handle) where T : UnityEngine.Object
        {
            _mainThread.AssertMainThread();
            if (handle == null)
            {
                return Stale<T>("Object handle is required.");
            }

            if (!string.Equals(handle.SessionId, _session.SessionId, StringComparison.Ordinal))
            {
                return RelayLiveLoopResult<T>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.WrongSession,
                        "resolve_object",
                        "Object handle belongs to another Player session.",
                        false));
            }

            long currentGeneration;
            if (!_scopeGenerations.TryGetValue(handle.ScopeId, out currentGeneration)) currentGeneration = 0;
            if (handle.Generation != currentGeneration)
            {
                return Stale<T>("Object handle generation is stale.");
            }

            Entry entry;
            if (!_entries.TryGetValue(handle.HandleId, out entry) ||
                entry.Generation != handle.Generation ||
                !string.Equals(entry.ScopeId, handle.ScopeId, StringComparison.Ordinal))
            {
                return Stale<T>("Object handle is missing or invalid.");
            }

            var value = entry.Target.Target as T;
            if (value == null)
            {
                _entries.Remove(handle.HandleId);
                return Stale<T>("Unity object was destroyed or collected.");
            }

            return RelayLiveLoopResult<T>.Success(value);
        }

        public void Remove(RuntimeObjectHandle handle)
        {
            _mainThread.AssertMainThread();
            if (handle != null) _entries.Remove(handle.HandleId);
        }

        public void Clear()
        {
            _mainThread.AssertMainThread();
            _entries.Clear();
            _scopeGenerations.Clear();
        }

        private void RemoveScopeEntries(string scopeId)
        {
            var remove = new List<string>();
            foreach (var pair in _entries)
            {
                if (string.Equals(pair.Value.ScopeId, scopeId, StringComparison.Ordinal)) remove.Add(pair.Key);
            }

            foreach (var id in remove) _entries.Remove(id);
        }

        private static RelayLiveLoopResult<T> Stale<T>(string message)
        {
            return RelayLiveLoopResult<T>.Failure(
                RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.StaleTarget, "resolve_object", message, true));
        }

        private static void ValidateScopeId(string scopeId)
        {
            RuntimeSessionIdentity.RequireCanonicalValue(scopeId, nameof(scopeId));
        }
    }
}
#endif
