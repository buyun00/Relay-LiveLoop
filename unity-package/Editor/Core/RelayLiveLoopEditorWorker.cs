#if UNITY_EDITOR
using System;
using System.Threading.Tasks;

namespace RelayLiveLoop
{
    public sealed class RelayLiveLoopEditorWorker
    {
        private readonly AtomicEditorJobStore _store;
        private readonly EditorJobProviderRegistry _providers;
        private readonly TimeSpan _pollBudget;
        private EditorJobClaim _claim;
        private IEditorJobOperation _operation;
        private Task<EditorArtifactSealResult> _sealTask;
        private EditorJobPollResult _completionToSeal;

        public RelayLiveLoopEditorWorker(
            AtomicEditorJobStore store,
            EditorJobProviderRegistry providers,
            TimeSpan? pollBudget = null)
        {
            _store = store ?? throw new ArgumentNullException(nameof(store));
            _providers = providers ?? throw new ArgumentNullException(nameof(providers));
            _pollBudget = pollBudget ?? TimeSpan.FromMilliseconds(4);
            if (_pollBudget <= TimeSpan.Zero || _pollBudget > TimeSpan.FromMilliseconds(50))
            {
                throw new ArgumentOutOfRangeException(nameof(pollBudget));
            }
        }

        public bool IsBusy { get { return _claim != null; } }
        public string CurrentJobId { get { return _claim == null ? null : _claim.Request.jobId; } }

        public void Tick()
        {
            if (_claim == null)
            {
                TryStartNext();
                return;
            }

            if (_sealTask != null)
            {
                PollArtifactSeal();
                return;
            }

            if (_operation == null)
            {
                FailCurrent("STATE_UNKNOWN", "editor_job", "No recoverable operation exists for the recorded attempt.", false);
                return;
            }

            EditorJobPollResult poll;
            try
            {
                poll = _operation.Poll(_pollBudget);
            }
            catch (Exception ex)
            {
                FailCurrent("INTERNAL_ERROR", "editor_job_poll", ex.GetType().Name + ": " + ex.Message, false);
                return;
            }

            if (poll == null)
            {
                FailCurrent("INTERNAL_ERROR", "editor_job_poll", "Provider returned no poll result.", false);
                return;
            }

            if (poll.State == EditorJobPollState.Running) return;
            if (poll.State == EditorJobPollState.Failed)
            {
                _store.PublishFailure(_claim, poll.Error ?? AtomicEditorJobStore.Error(
                    "INTERNAL_ERROR", "editor_job", "Provider failed without an error.", false));
                ClearCurrent();
                return;
            }

            _completionToSeal = poll;
            _sealTask = _store.SealArtifactsAsync(_claim, poll.Artifacts);
        }

        private void TryStartNext()
        {
            var claim = _store.TryGetRecovery(CanRun) ?? _store.TryClaimNext(CanRun);
            if (claim == null) return;
            _claim = claim;
            if (!string.IsNullOrEmpty(claim.RecoveryProblem))
            {
                _store.BeginAttempt(claim);
                FailCurrent("STATE_UNKNOWN", "domain_reload_recovery", claim.RecoveryProblem, false);
                return;
            }

            var attempt = _store.BeginAttempt(claim);
            IEditorJobProvider provider;
            if (!_providers.TryResolve(claim.Request.providerId, claim.Request.kind, out provider))
            {
                // Registry may be repopulated later in the same domain reload. Leave the durable
                // processing record untouched and retry on a later update.
                ClearCurrent();
                return;
            }

            var execution = new EditorJobExecution(claim.Request, attempt, claim.IsRecovering);
            try
            {
                _operation = claim.IsRecovering
                    ? provider.Recover(execution)
                    : provider.Begin(execution);
            }
            catch (Exception ex)
            {
                FailCurrent("INTERNAL_ERROR", "editor_job_begin", ex.GetType().Name + ": " + ex.Message, false);
                return;
            }

            if (_operation == null)
            {
                FailCurrent(
                    claim.IsRecovering ? "STATE_UNKNOWN" : "INTERNAL_ERROR",
                    claim.IsRecovering ? "domain_reload_recovery" : "editor_job_begin",
                    claim.IsRecovering
                        ? "Provider cannot reconcile the recorded attempt; it was not replayed."
                        : "Provider did not return an operation.",
                    false);
            }
        }

        private bool CanRun(EditorJobRequest request)
        {
            IEditorJobProvider ignored;
            return _providers.TryResolve(request.providerId, request.kind, out ignored);
        }

        private void PollArtifactSeal()
        {
            if (!_sealTask.IsCompleted) return;
            if (_sealTask.IsFaulted || _sealTask.IsCanceled)
            {
                var message = _sealTask.Exception == null
                    ? "Artifact sealing was cancelled."
                    : _sealTask.Exception.GetBaseException().Message;
                FailCurrent("INTERNAL_ERROR", "seal_artifacts", message, true);
                return;
            }

            var sealedResult = _sealTask.Result;
            if (sealedResult.Error != null)
            {
                _store.PublishFailure(_claim, sealedResult.Error);
            }
            else
            {
                _store.PublishSuccess(_claim, _completionToSeal.ResultJson, sealedResult.Artifacts);
            }

            ClearCurrent();
        }

        private void FailCurrent(string code, string stage, string message, bool recoverable)
        {
            _store.PublishFailure(_claim, AtomicEditorJobStore.Error(code, stage, message, recoverable));
            ClearCurrent();
        }

        private void ClearCurrent()
        {
            _claim = null;
            _operation = null;
            _sealTask = null;
            _completionToSeal = null;
        }
    }
}
#endif
