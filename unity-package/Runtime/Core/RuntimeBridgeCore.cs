#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace RelayLiveLoop
{
    public sealed class RuntimeBridgeLimits
    {
        public RuntimeBridgeLimits(
            int maximumMessageBytes = 64 * 1024,
            int maximumTrackedRequests = 256,
            TimeSpan? mainThreadTimeout = null)
        {
            if (maximumMessageBytes < 256) throw new ArgumentOutOfRangeException(nameof(maximumMessageBytes));
            if (maximumTrackedRequests < 8) throw new ArgumentOutOfRangeException(nameof(maximumTrackedRequests));
            var timeout = mainThreadTimeout ?? TimeSpan.FromSeconds(15);
            if (timeout <= TimeSpan.Zero || timeout > TimeSpan.FromMinutes(5))
            {
                throw new ArgumentOutOfRangeException(nameof(mainThreadTimeout));
            }

            MaximumMessageBytes = maximumMessageBytes;
            MaximumTrackedRequests = maximumTrackedRequests;
            MainThreadTimeout = timeout;
        }

        public int MaximumMessageBytes { get; private set; }
        public int MaximumTrackedRequests { get; private set; }
        public TimeSpan MainThreadTimeout { get; private set; }
    }

    public sealed class AuthenticatedRuntimeRequest
    {
        public AuthenticatedRuntimeRequest(
            string expectedSessionId,
            string expectedRuntimeRevision,
            RequestAuthentication authentication,
            byte[] payload)
        {
            ExpectedSessionId = expectedSessionId;
            ExpectedRuntimeRevision = expectedRuntimeRevision;
            Authentication = authentication;
            Payload = payload;
        }

        public string ExpectedSessionId { get; private set; }
        public string ExpectedRuntimeRevision { get; private set; }
        public RequestAuthentication Authentication { get; private set; }
        public byte[] Payload { get; private set; }
    }

    public sealed class RuntimeBridgeCore : IDisposable
    {
        private sealed class TrackedRequest
        {
            public string PayloadHash;
            public Task<RelayLiveLoopResult> Task;
        }

        private readonly object _requestsSync = new object();
        private readonly RuntimeSessionIdentity _identity;
        private readonly RelayLiveLoopMainThreadDispatcher _dispatcher;
        private readonly RuntimeBridgeLimits _limits;
        private readonly Dictionary<string, TrackedRequest> _requests =
            new Dictionary<string, TrackedRequest>(StringComparer.Ordinal);
        private readonly Queue<string> _requestOrder = new Queue<string>();
        private bool _disposed;

        public RuntimeBridgeCore(
            RuntimeSessionIdentity identity,
            byte[] sharedSecret,
            RelayLiveLoopMainThreadDispatcher dispatcher,
            RuntimeBridgeLimits limits = null)
        {
            _identity = identity ?? throw new ArgumentNullException(nameof(identity));
            _dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
            _dispatcher.AssertMainThread();
            _limits = limits ?? new RuntimeBridgeLimits();
            RuntimeRevisions = identity.BindRuntimeRevisionAuthority(dispatcher);
            Authentication = new SessionAuthentication(identity, sharedSecret);
            Framer = new BoundedMessageFramer(_limits.MaximumMessageBytes);
            ObjectHandles = new ObjectHandleRegistry(identity, dispatcher);
            Providers = new ProviderRegistry(dispatcher);
        }

        public SessionAuthentication Authentication { get; private set; }
        public BoundedMessageFramer Framer { get; private set; }
        public ObjectHandleRegistry ObjectHandles { get; private set; }
        public ProviderRegistry Providers { get; private set; }
        public RuntimeRevisionClock RuntimeRevisions { get; private set; }
        public RuntimeSessionIdentity Identity { get { return _identity; } }

        public RelayLiveLoopResult<RuntimeRevisionTransition> PublishAuthorizedCompletedRuntimeTransition(
            string operationId,
            string expectedRuntimeRevision,
            string runtimeRevisionAfter)
        {
            if (_disposed) throw new ObjectDisposedException(nameof(RuntimeBridgeCore));
            return RuntimeRevisions.PublishAuthorizedCompletedTransition(
                operationId,
                expectedRuntimeRevision,
                runtimeRevisionAfter);
        }

        public Task<RelayLiveLoopResult> ScheduleAuthenticated(
            AuthenticatedRuntimeRequest request,
            Func<AuthenticatedRuntimeRequest, RelayLiveLoopResult> mainThreadHandler,
            CancellationToken cancellationToken)
        {
            if (_disposed) throw new ObjectDisposedException(nameof(RuntimeBridgeCore));
            if (request == null || request.Authentication == null || request.Payload == null)
            {
                return FailureTask(RelayLiveLoopErrorCode.InvalidMessage, "validate_request", "Request fields are incomplete.", false);
            }

            if (mainThreadHandler == null) throw new ArgumentNullException(nameof(mainThreadHandler));
            if (request.Payload.Length <= 0 || request.Payload.Length > _limits.MaximumMessageBytes)
            {
                return FailureTask(
                    request.Payload.Length > _limits.MaximumMessageBytes
                        ? RelayLiveLoopErrorCode.MessageTooLarge
                        : RelayLiveLoopErrorCode.InvalidMessage,
                    "validate_request",
                    "Payload length is outside the configured bounds.",
                    false);
            }

            if (!string.Equals(request.ExpectedSessionId, _identity.SessionId, StringComparison.Ordinal))
            {
                return FailureTask(RelayLiveLoopErrorCode.WrongSession, "validate_request", "Request targets another Player session.", false);
            }

            var revision = RuntimeRevisions.CaptureIfCurrent(
                request.ExpectedRuntimeRevision,
                "validate_request");
            if (!revision.Succeeded) return FailureTask(revision.Error);

            var payloadHash = SessionAuthentication.ComputeSha256(request.Payload);
            if (!string.Equals(payloadHash, request.Authentication.PayloadSha256, StringComparison.OrdinalIgnoreCase))
            {
                return FailureTask(RelayLiveLoopErrorCode.ContractMismatch, "validate_request", "Authenticated payload hash does not match the body.", false);
            }

            var authResult = Authentication.AuthenticateRequest(request.Authentication);
            if (!authResult.Succeeded) return Task.FromResult(authResult);

            lock (_requestsSync)
            {
                TrackedRequest tracked;
                if (_requests.TryGetValue(request.Authentication.RequestId, out tracked))
                {
                    if (!string.Equals(tracked.PayloadHash, payloadHash, StringComparison.OrdinalIgnoreCase))
                    {
                        return FailureTask(
                            RelayLiveLoopErrorCode.InputChanged,
                            "idempotency",
                            "Request id was reused with a different payload.",
                            false);
                    }

                    return tracked.Task;
                }

                TrimTrackedRequests();
                if (_requests.Count >= _limits.MaximumTrackedRequests)
                {
                    return FailureTask(
                        RelayLiveLoopErrorCode.Busy,
                        "idempotency",
                        "Tracked request capacity is full while earlier requests are still running.",
                        true);
                }

                var task = _dispatcher.Schedule(
                    request.Authentication.RequestId,
                    () => ExecuteOnMainThread(request, mainThreadHandler),
                    _limits.MainThreadTimeout,
                    cancellationToken);
                _requests.Add(request.Authentication.RequestId, new TrackedRequest
                {
                    PayloadHash = payloadHash,
                    Task = task
                });
                _requestOrder.Enqueue(request.Authentication.RequestId);
                return task;
            }
        }

        public void Dispose()
        {
            if (_disposed) return;
            _dispatcher.AssertMainThread();
            Providers.Clear();
            ObjectHandles.Clear();
            Authentication.Dispose();
            lock (_requestsSync)
            {
                _requests.Clear();
                _requestOrder.Clear();
            }

            _disposed = true;
        }

        private RelayLiveLoopResult ExecuteOnMainThread(
            AuthenticatedRuntimeRequest request,
            Func<AuthenticatedRuntimeRequest, RelayLiveLoopResult> mainThreadHandler)
        {
            _dispatcher.AssertMainThread();
            var revision = RuntimeRevisions.CaptureIfCurrent(
                request.ExpectedRuntimeRevision,
                "main_thread_execute");
            if (!revision.Succeeded) return RelayLiveLoopResult.Failure(revision.Error);
            return mainThreadHandler(request);
        }

        private void TrimTrackedRequests()
        {
            while (_requests.Count >= _limits.MaximumTrackedRequests && _requestOrder.Count > 0)
            {
                var oldest = _requestOrder.Peek();
                TrackedRequest tracked;
                if (!_requests.TryGetValue(oldest, out tracked))
                {
                    _requestOrder.Dequeue();
                    continue;
                }

                if (!tracked.Task.IsCompleted) break;
                _requestOrder.Dequeue();
                _requests.Remove(oldest);
            }
        }

        private static Task<RelayLiveLoopResult> FailureTask(RelayLiveLoopError error)
        {
            return Task.FromResult(RelayLiveLoopResult.Failure(error));
        }

        private static Task<RelayLiveLoopResult> FailureTask(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable)
        {
            return FailureTask(RelayLiveLoopErrors.Create(code, stage, message, recoverable));
        }
    }
}
#endif
