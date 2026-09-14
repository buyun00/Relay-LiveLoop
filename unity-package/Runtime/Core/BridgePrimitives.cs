#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    public enum RelayLiveLoopErrorCode
    {
        None = 0,
        CapabilityUnavailable,
        AuthRequired,
        ContractMismatch,
        WrongSession,
        StaleTarget,
        InputChanged,
        CompileFailed,
        ResourceBuildFailed,
        ApprovalRequired,
        UnloadRefused,
        RestoreFailed,
        StateUnknown,
        InvalidMessage,
        MessageTooLarge,
        Timeout,
        Busy,
        InternalError
    }

    public sealed class RelayLiveLoopError
    {
        public RelayLiveLoopError(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool? runtimeChanged,
            bool recoverable,
            IReadOnlyList<string> evidence)
        {
            Code = code;
            Stage = stage ?? string.Empty;
            Message = message ?? string.Empty;
            RuntimeChanged = runtimeChanged;
            Recoverable = recoverable;
            Evidence = evidence ?? Array.Empty<string>();
        }

        public RelayLiveLoopErrorCode Code { get; private set; }
        public string Stage { get; private set; }
        public string Message { get; private set; }
        public bool? RuntimeChanged { get; private set; }
        public bool Recoverable { get; private set; }
        public IReadOnlyList<string> Evidence { get; private set; }
    }

    public class RelayLiveLoopResult
    {
        protected RelayLiveLoopResult(bool succeeded, RelayLiveLoopError error)
        {
            Succeeded = succeeded;
            Error = error;
        }

        public bool Succeeded { get; private set; }
        public RelayLiveLoopError Error { get; private set; }

        public static RelayLiveLoopResult Success()
        {
            return new RelayLiveLoopResult(true, null);
        }

        public static RelayLiveLoopResult Failure(RelayLiveLoopError error)
        {
            if (error == null) throw new ArgumentNullException(nameof(error));
            return new RelayLiveLoopResult(false, error);
        }
    }

    public sealed class RelayLiveLoopResult<T> : RelayLiveLoopResult
    {
        private RelayLiveLoopResult(bool succeeded, T value, RelayLiveLoopError error)
            : base(succeeded, error)
        {
            Value = value;
        }

        public T Value { get; private set; }

        public static RelayLiveLoopResult<T> Success(T value)
        {
            return new RelayLiveLoopResult<T>(true, value, null);
        }

        public new static RelayLiveLoopResult<T> Failure(RelayLiveLoopError error)
        {
            if (error == null) throw new ArgumentNullException(nameof(error));
            return new RelayLiveLoopResult<T>(false, default(T), error);
        }
    }

    internal static class RelayLiveLoopErrors
    {
        public static RelayLiveLoopError Create(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable,
            bool? runtimeChanged = false)
        {
            return new RelayLiveLoopError(
                code,
                stage,
                message,
                runtimeChanged,
                recoverable,
                Array.Empty<string>());
        }
    }
}
#endif
