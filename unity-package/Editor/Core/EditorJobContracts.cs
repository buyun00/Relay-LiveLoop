#if UNITY_EDITOR
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    [Serializable]
    public sealed class EditorJobRequest
    {
        public string jobId;
        public string kind;
        public string inputSnapshot;
        public string providerId;
        public string artifactRoot;
        public string requestedAtUtc;
        public string expiresAtUtc;
        public string payloadJson;
    }

    [Serializable]
    public sealed class EditorJobAttemptRecord
    {
        public string jobId;
        public string requestDigest;
        public string inputSnapshot;
        public string providerId;
        public string attemptId;
        public string attemptRoot;
        public string startedAtUtc;
        public string state;
    }

    [Serializable]
    public sealed class EditorJobArtifact
    {
        public string artifactId;
        public string kind;
        public string path;
        public string sha256;
        public string mediaType;
        public long size;
    }

    [Serializable]
    public sealed class EditorJobError
    {
        public string code;
        public string stage;
        public string message;
        public bool recoverable;
        public bool runtimeChangedKnown;
        public bool runtimeChanged;
    }

    [Serializable]
    public sealed class EditorJobResult
    {
        public string jobId;
        public string requestDigest;
        public string inputSnapshot;
        public string providerId;
        public string attemptId;
        public string status;
        public string completedAtUtc;
        public string resultJson;
        public EditorJobError error;
        public List<EditorJobArtifact> artifacts = new List<EditorJobArtifact>();
    }

    public enum EditorJobPollState
    {
        Running,
        Completed,
        Failed
    }

    public sealed class EditorJobPollResult
    {
        private EditorJobPollResult(
            EditorJobPollState state,
            string resultJson,
            IReadOnlyList<EditorJobArtifact> artifacts,
            EditorJobError error)
        {
            State = state;
            ResultJson = resultJson;
            Artifacts = artifacts ?? Array.Empty<EditorJobArtifact>();
            Error = error;
        }

        public EditorJobPollState State { get; private set; }
        public string ResultJson { get; private set; }
        public IReadOnlyList<EditorJobArtifact> Artifacts { get; private set; }
        public EditorJobError Error { get; private set; }

        public static EditorJobPollResult Running()
        {
            return new EditorJobPollResult(EditorJobPollState.Running, null, null, null);
        }

        public static EditorJobPollResult Completed(
            string resultJson,
            IReadOnlyList<EditorJobArtifact> artifacts)
        {
            return new EditorJobPollResult(EditorJobPollState.Completed, resultJson, artifacts, null);
        }

        public static EditorJobPollResult Failed(EditorJobError error)
        {
            if (error == null) throw new ArgumentNullException(nameof(error));
            return new EditorJobPollResult(EditorJobPollState.Failed, null, null, error);
        }
    }

    public sealed class EditorJobExecution
    {
        internal EditorJobExecution(
            EditorJobRequest request,
            EditorJobAttemptRecord attempt,
            bool recovering)
        {
            Request = request;
            Attempt = attempt;
            IsRecoveringAfterDomainReload = recovering;
        }

        public EditorJobRequest Request { get; private set; }
        public EditorJobAttemptRecord Attempt { get; private set; }
        public bool IsRecoveringAfterDomainReload { get; private set; }
    }

    public interface IEditorJobOperation
    {
        EditorJobPollResult Poll(TimeSpan mainThreadBudget);
    }

    public interface IEditorJobProvider
    {
        string ProviderId { get; }
        bool Supports(string kind);

        // Begin is called only after an attempt record is durably written.
        IEditorJobOperation Begin(EditorJobExecution execution);

        // Recover must reconcile the recorded attempt. Returning null means the provider cannot
        // prove whether an earlier attempt changed state; the worker emits STATE_UNKNOWN and does
        // not rerun it under a new attempt.
        IEditorJobOperation Recover(EditorJobExecution execution);
    }
}
#endif
