#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    /// <summary>
    /// Plain-data request passed to the V-owned operation binding on Unity's main thread.
    /// The payload is an exact copy of the authenticated wire bytes.
    /// </summary>
    public sealed class RuntimeTransportCommandContext
    {
        private readonly byte[] _payload;

        internal RuntimeTransportCommandContext(
            string requestId,
            string operation,
            string expectedSessionId,
            string expectedRuntimeRevision,
            byte[] payload)
        {
            RequestId = requestId;
            Operation = operation;
            ExpectedSessionId = expectedSessionId;
            ExpectedRuntimeRevision = expectedRuntimeRevision;
            _payload = (byte[])payload.Clone();
        }

        public string RequestId { get; private set; }
        public string Operation { get; private set; }
        public string ExpectedSessionId { get; private set; }
        public string ExpectedRuntimeRevision { get; private set; }
        public byte[] Payload { get { return (byte[])_payload.Clone(); } }
    }

    /// <summary>
    /// Small neutral binding point. V supplies one stable adapter that routes operation names
    /// to the existing provider hubs; project and SDK types never cross this interface.
    /// </summary>
    public interface IRuntimeTransportCommandHandler
    {
        RelayLiveLoopResult<NeutralPayload> Execute(RuntimeTransportCommandContext request);
    }

    /// <summary>
    /// Optional asynchronous companion for handlers whose main-thread operation must yield,
    /// such as a one-frame UI focus boundary. It shares the existing bridge and idempotency
    /// path; it is not a second transport or command router.
    /// </summary>
    public interface IRuntimeTransportAsyncCommandHandler
    {
        System.Threading.Tasks.Task<RelayLiveLoopResult<NeutralPayload>> ExecuteAsync(
            RuntimeTransportCommandContext request,
            System.Threading.CancellationToken cancellationToken);
    }

    /// <summary>
    /// The concrete result stored by RuntimeBridgeCore's existing request-id tracker. Keeping
    /// the payload on the result object means an exact duplicate shares the original result
    /// Task and bytes without introducing another idempotency cache or ledger.
    /// </summary>
    public sealed class RuntimeTransportExecutionResult : RelayLiveLoopResult
    {
        private RuntimeTransportExecutionResult(
            bool succeeded,
            NeutralPayload payload,
            bool runtimeChanged,
            string runtimeRevisionAfter,
            RelayLiveLoopError error)
            : base(succeeded, error)
        {
            Payload = payload;
            RuntimeChanged = runtimeChanged;
            RuntimeRevisionAfter = runtimeRevisionAfter;
        }

        public NeutralPayload Payload { get; private set; }
        public bool RuntimeChanged { get; private set; }
        public string RuntimeRevisionAfter { get; private set; }

        internal static RuntimeTransportExecutionResult Completed(
            NeutralPayload payload,
            bool runtimeChanged,
            string runtimeRevisionAfter)
        {
            if (payload == null) throw new ArgumentNullException(nameof(payload));
            return new RuntimeTransportExecutionResult(
                true,
                payload,
                runtimeChanged,
                runtimeRevisionAfter,
                null);
        }

        internal static RuntimeTransportExecutionResult Failed(
            RelayLiveLoopError error,
            string runtimeRevisionAfter)
        {
            if (error == null) throw new ArgumentNullException(nameof(error));
            return new RuntimeTransportExecutionResult(
                false,
                null,
                error.RuntimeChanged == true,
                runtimeRevisionAfter,
                error);
        }
    }

    /// <summary>
    /// Compileable neutral adapter from the TCP transport to RuntimeBridgeCore. Authentication,
    /// session/revision validation, payload hashing, sequence defense, request-id matching and
    /// main-thread scheduling remain owned by RuntimeBridgeCore.
    /// </summary>
    public sealed class RuntimeTransportCommandAdapter
    {
        private readonly RuntimeBridgeCore _bridge;
        private readonly IRuntimeTransportCommandHandler _handler;
        private readonly IRuntimeTransportAsyncCommandHandler _asyncHandler;

        public RuntimeTransportCommandAdapter(
            RuntimeBridgeCore bridge,
            IRuntimeTransportCommandHandler handler)
        {
            _bridge = bridge ?? throw new ArgumentNullException(nameof(bridge));
            _handler = handler ?? throw new ArgumentNullException(nameof(handler));
        }

        public RuntimeTransportCommandAdapter(
            RuntimeBridgeCore bridge,
            IRuntimeTransportCommandHandler handler,
            IRuntimeTransportAsyncCommandHandler asyncHandler)
            : this(bridge, handler)
        {
            _asyncHandler = asyncHandler ?? throw new ArgumentNullException(nameof(asyncHandler));
        }

        public System.Threading.Tasks.Task<RelayLiveLoopResult> Schedule(
            AuthenticatedRuntimeRequest request,
            System.Threading.CancellationToken cancellationToken)
        {
            return _bridge.ScheduleAuthenticated(request, ExecuteOnMainThread, cancellationToken);
        }

        public System.Threading.Tasks.Task<RelayLiveLoopResult> ScheduleAsync(
            AuthenticatedRuntimeRequest request,
            System.Threading.CancellationToken cancellationToken)
        {
            if (_asyncHandler == null) return Schedule(request, cancellationToken);
            return _bridge.ScheduleAuthenticatedAsync(request, ExecuteOnMainThreadAsync, cancellationToken);
        }

        private RelayLiveLoopResult ExecuteOnMainThread(AuthenticatedRuntimeRequest request)
        {
            var revisionBefore = _bridge.Identity.RuntimeRevision;
            RelayLiveLoopResult<NeutralPayload> result;
            try
            {
                result = _handler.Execute(new RuntimeTransportCommandContext(
                    request.Authentication.RequestId,
                    request.Authentication.Operation,
                    request.ExpectedSessionId,
                    request.ExpectedRuntimeRevision,
                    request.Payload));
            }
            catch (Exception exception)
            {
                var revisionAfterException = _bridge.Identity.RuntimeRevision;
                var changed = !string.Equals(
                    revisionBefore,
                    revisionAfterException,
                    StringComparison.Ordinal);
                var details = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    { "exceptionType", exception.GetType().Name }
                };
                var error = RelayLiveLoopErrors.Create(
                    changed ? RelayLiveLoopErrorCode.StateUnknown : RelayLiveLoopErrorCode.InternalError,
                    "runtime_transport_execute",
                    changed
                        ? "The operation handler threw after the runtime revision changed."
                        : "The operation handler threw before reporting a contract result: " + exception.Message,
                    false,
                    changed ? (bool?)true : null,
                    details);
                return RuntimeTransportExecutionResult.Failed(error, revisionAfterException);
            }

            var revisionAfter = _bridge.Identity.RuntimeRevision;
            var runtimeChanged = !string.Equals(
                revisionBefore,
                revisionAfter,
                StringComparison.Ordinal);
            if (result == null)
            {
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        "runtime_transport_execute",
                        "The operation handler returned no result.",
                        false,
                        runtimeChanged ? (bool?)true : false),
                    revisionAfter);
            }

            if (result.Succeeded)
            {
                if (result.Value == null)
                {
                    return RuntimeTransportExecutionResult.Failed(
                        RelayLiveLoopErrors.Create(
                            RelayLiveLoopErrorCode.ContractMismatch,
                            "runtime_transport_execute",
                            "A successful operation handler result requires a neutral payload.",
                            false,
                            runtimeChanged ? (bool?)true : false),
                        revisionAfter);
                }

                return RuntimeTransportExecutionResult.Completed(
                    result.Value,
                    runtimeChanged,
                    revisionAfter);
            }

            if (result.Error == null)
            {
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        "runtime_transport_execute",
                        "A failed operation handler result requires an error.",
                        false,
                        runtimeChanged ? (bool?)true : false),
                    revisionAfter);
            }

            if (runtimeChanged && result.Error.RuntimeChanged != true)
            {
                var details = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    { "reportedCode", result.Error.Code.ToString() },
                    { "reportedStage", result.Error.Stage }
                };
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "runtime_transport_execute",
                        "The handler failed after the runtime revision changed but did not report that mutation.",
                        false,
                        true,
                        details),
                    revisionAfter);
            }

            if (!runtimeChanged && result.Error.RuntimeChanged == true)
            {
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(
                        RelayLiveLoopErrorCode.ContractMismatch,
                        "runtime_transport_execute",
                        "The handler reported a runtime mutation without publishing a new runtime revision.",
                        false,
                        false),
                    revisionAfter);
            }

            return RuntimeTransportExecutionResult.Failed(result.Error, revisionAfter);
        }

        private async System.Threading.Tasks.Task<RelayLiveLoopResult> ExecuteOnMainThreadAsync(
            AuthenticatedRuntimeRequest request,
            System.Threading.CancellationToken cancellationToken)
        {
            var revisionBefore = _bridge.Identity.RuntimeRevision;
            RelayLiveLoopResult<NeutralPayload> result;
            try
            {
                result = await _asyncHandler.ExecuteAsync(new RuntimeTransportCommandContext(
                    request.Authentication.RequestId,
                    request.Authentication.Operation,
                    request.ExpectedSessionId,
                    request.ExpectedRuntimeRevision,
                    request.Payload), cancellationToken);
            }
            catch (Exception exception)
            {
                var changed = !string.Equals(revisionBefore, _bridge.Identity.RuntimeRevision, StringComparison.Ordinal);
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(
                        changed ? RelayLiveLoopErrorCode.StateUnknown : RelayLiveLoopErrorCode.InternalError,
                        "runtime_transport_execute",
                        changed ? "The operation handler threw after the runtime revision changed." :
                            "The operation handler threw before reporting a contract result: " + exception.Message,
                        false, changed ? (bool?)true : null,
                        new Dictionary<string, string> { { "exceptionType", exception.GetType().Name } }),
                    _bridge.Identity.RuntimeRevision);
            }

            var revisionAfter = _bridge.Identity.RuntimeRevision;
            var runtimeChanged = !string.Equals(revisionBefore, revisionAfter, StringComparison.Ordinal);
            if (result == null)
            {
                return RuntimeTransportExecutionResult.Failed(
                    RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.ContractMismatch,
                        "runtime_transport_execute", "The operation handler returned no result.", false,
                        runtimeChanged ? (bool?)true : false), revisionAfter);
            }
            if (result.Succeeded && result.Value != null)
            {
                return RuntimeTransportExecutionResult.Completed(result.Value, runtimeChanged, revisionAfter);
            }
            if (!result.Succeeded && result.Error != null)
            {
                if (runtimeChanged && result.Error.RuntimeChanged != true)
                {
                    return RuntimeTransportExecutionResult.Failed(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.StateUnknown,
                            "runtime_transport_execute", "The handler failed after the runtime revision changed but did not report that mutation.", false, true), revisionAfter);
                }
                if (!runtimeChanged && result.Error.RuntimeChanged == true)
                {
                    return RuntimeTransportExecutionResult.Failed(
                        RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.ContractMismatch,
                            "runtime_transport_execute", "The handler reported a runtime mutation without publishing a new runtime revision.", false, false), revisionAfter);
                }
                return RuntimeTransportExecutionResult.Failed(result.Error, revisionAfter);
            }
            return RuntimeTransportExecutionResult.Failed(
                RelayLiveLoopErrors.Create(RelayLiveLoopErrorCode.ContractMismatch,
                    "runtime_transport_execute", "The asynchronous handler returned an invalid result.", false,
                    runtimeChanged ? (bool?)true : false), revisionAfter);
        }
    }
}
#endif

