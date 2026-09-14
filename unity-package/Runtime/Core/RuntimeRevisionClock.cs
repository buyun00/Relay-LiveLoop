#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    public interface IRuntimeRevisionSource
    {
        string CurrentRevision { get; }

        RelayLiveLoopResult<string> CaptureIfCurrent(
            string expectedRuntimeRevision,
            string stage);
    }

    public sealed class RuntimeRevisionTransition
    {
        internal RuntimeRevisionTransition(
            RuntimeRevisionClock authority,
            string operationId,
            string previousRevision,
            string runtimeRevisionAfter)
        {
            Authority = authority;
            OperationId = operationId;
            PreviousRevision = previousRevision;
            RuntimeRevisionAfter = runtimeRevisionAfter;
        }

        internal RuntimeRevisionClock Authority { get; private set; }
        public string OperationId { get; private set; }
        public string PreviousRevision { get; private set; }
        public string RuntimeRevisionAfter { get; private set; }
    }

    public sealed class RuntimeRevisionClock : IRuntimeRevisionSource
    {
        private readonly object _sync = new object();
        private readonly HashSet<string> _publishedRevisions;
        private string _currentRevision;
        private IRelayLiveLoopMainThreadGuard _mainThread;

        internal RuntimeRevisionClock(string initialRevision)
        {
            _currentRevision = RuntimeSessionIdentity.RequireCanonicalValue(
                initialRevision,
                nameof(initialRevision));
            _publishedRevisions = new HashSet<string>(StringComparer.Ordinal)
            {
                _currentRevision
            };
        }

        public string CurrentRevision
        {
            get
            {
                lock (_sync) return _currentRevision;
            }
        }

        public RelayLiveLoopResult<string> CaptureIfCurrent(
            string expectedRuntimeRevision,
            string stage)
        {
            try
            {
                RuntimeSessionIdentity.RequireCanonicalValue(
                    expectedRuntimeRevision,
                    nameof(expectedRuntimeRevision));
                RuntimeSessionIdentity.RequireCanonicalValue(stage, nameof(stage));
            }
            catch (ArgumentException ex)
            {
                return RelayLiveLoopResult<string>.Failure(RelayLiveLoopErrors.Create(
                    RelayLiveLoopErrorCode.InvalidMessage,
                    string.IsNullOrWhiteSpace(stage) ? "runtime_revision" : stage,
                    ex.Message,
                    false));
            }

            lock (_sync)
            {
                if (!string.Equals(expectedRuntimeRevision, _currentRevision, StringComparison.Ordinal))
                {
                    return RelayLiveLoopResult<string>.Failure(RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StaleTarget,
                        stage,
                        "Expected runtime revision does not match the running bridge.",
                        true));
                }

                return RelayLiveLoopResult<string>.Success(_currentRevision);
            }
        }

        public RelayLiveLoopResult<RuntimeRevisionTransition> PublishAuthorizedCompletedTransition(
            string operationId,
            string expectedRuntimeRevision,
            string runtimeRevisionAfter)
        {
            IRelayLiveLoopMainThreadGuard mainThread;
            lock (_sync) mainThread = _mainThread;
            if (mainThread == null)
            {
                throw new InvalidOperationException(
                    "Runtime revision authority is not bound to the bridge main thread.");
            }

            mainThread.AssertMainThread();

            try
            {
                RuntimeSessionIdentity.RequireCanonicalValue(operationId, nameof(operationId));
                RuntimeSessionIdentity.RequireCanonicalValue(
                    expectedRuntimeRevision,
                    nameof(expectedRuntimeRevision));
                RuntimeSessionIdentity.RequireCanonicalValue(
                    runtimeRevisionAfter,
                    nameof(runtimeRevisionAfter));
            }
            catch (ArgumentException ex)
            {
                return RelayLiveLoopResult<RuntimeRevisionTransition>.Failure(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.InvalidMessage,
                        "runtime_revision_transition",
                        ex.Message,
                        false));
            }

            lock (_sync)
            {
                if (!string.Equals(expectedRuntimeRevision, _currentRevision, StringComparison.Ordinal))
                {
                    return RelayLiveLoopResult<RuntimeRevisionTransition>.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.StaleTarget,
                            "runtime_revision_transition",
                            "The completed operation targeted a stale runtime revision.",
                            true));
                }

                if (_publishedRevisions.Contains(runtimeRevisionAfter))
                {
                    return RelayLiveLoopResult<RuntimeRevisionTransition>.Failure(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.ContractMismatch,
                            "runtime_revision_transition",
                            "A completed runtime change must publish a new caller-supplied revision that was not used earlier in this session.",
                            false));
                }

                var previous = _currentRevision;
                _currentRevision = runtimeRevisionAfter;
                _publishedRevisions.Add(runtimeRevisionAfter);
                return RelayLiveLoopResult<RuntimeRevisionTransition>.Success(
                    new RuntimeRevisionTransition(this, operationId, previous, runtimeRevisionAfter));
            }
        }

        internal void BindMainThreadAuthority(IRelayLiveLoopMainThreadGuard mainThread)
        {
            if (mainThread == null) throw new ArgumentNullException(nameof(mainThread));
            mainThread.AssertMainThread();
            lock (_sync)
            {
                if (_mainThread != null && !ReferenceEquals(_mainThread, mainThread))
                {
                    throw new InvalidOperationException(
                        "Runtime revision authority is already bound to another main-thread guard.");
                }

                _mainThread = mainThread;
            }
        }
    }
}
#endif
