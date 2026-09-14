#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using RelayLiveLoop;
using UnityEngine;

internal static class RuntimeTruthRepairSyntheticTests
{
    private static int _passed;

    public static int Main()
    {
        try
        {
            TestRevisionTransitionAndSharedReaders();
            TestEditorProviderFailureClassification();
            TestDurableSourceEditUnknownDoesNotReplay();
            TestPreviewConflictReportsNoWriteAndExternalDrift();
            Console.WriteLine("SYNTHETIC REPAIR PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SYNTHETIC REPAIR FAIL: " + ex);
            return 1;
        }
    }

    private static void TestRevisionTransitionAndSharedReaders()
    {
        var artifactRoot = Path.Combine(
            Path.GetTempPath(),
            "relay-liveloop-revision-frame-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(artifactRoot);
            Time.Reset(200);
            var dispatcher = new RelayLiveLoopMainThreadDispatcher();
            dispatcher.InitializeOnMainThread(16, 16, 50);
            var identity = new RuntimeSessionIdentity(
                "session-synthetic",
                "launch-synthetic",
                "revision-before",
                1);
            var secret = Secret();

            using (var bridge = new RuntimeBridgeCore(
                identity,
                secret,
                dispatcher,
                new RuntimeBridgeLimits(1024, 16, TimeSpan.FromSeconds(2))))
            {
                var now = DateTimeOffset.UtcNow;
                var pendingChallenge = bridge.Authentication.BeginHandshake(
                    identity.SessionId,
                    identity.LaunchId,
                    "revision-before",
                    identity.ProtocolVersion,
                    "pending-client").Value;
                var challenge = bridge.Authentication.BeginHandshake(
                    identity.SessionId,
                    identity.LaunchId,
                    "revision-before",
                    identity.ProtocolVersion,
                    "active-client").Value;
                var key = SessionAuthentication.DeriveConnectionKey(secret, identity, challenge);
                var connection = bridge.Authentication.CompleteHandshake(
                    challenge.ChallengeId,
                    SessionAuthentication.ComputeHandshakeProof(secret, identity, challenge)).Value;

                var firstPayload = Encoding.UTF8.GetBytes("{\"step\":1}");
                var secondPayload = Encoding.UTF8.GetBytes("{\"step\":2}");
                RuntimeRevisionTransition receipt = null;
                var runtimeValue = 0;
                var staleHandlerCalls = 0;
                var first = bridge.ScheduleAuthenticated(
                    new AuthenticatedRuntimeRequest(
                        identity.SessionId,
                        "revision-before",
                        SignedRequest(key, connection.ConnectionId, 1, now, "change-request", "iterate.apply", firstPayload),
                        firstPayload),
                    ignored =>
                    {
                        runtimeValue++;
                        var transition = bridge.PublishAuthorizedCompletedRuntimeTransition(
                            "change-request",
                            "revision-before",
                            "revision-after");
                        if (!transition.Succeeded) return RelayLiveLoopResult.Failure(transition.Error);
                        receipt = transition.Value;
                        return RelayLiveLoopResult.Success();
                    },
                    CancellationToken.None);
                var queuedStale = bridge.ScheduleAuthenticated(
                    new AuthenticatedRuntimeRequest(
                        identity.SessionId,
                        "revision-before",
                        SignedRequest(key, connection.ConnectionId, 2, now, "queued-stale", "observe", secondPayload),
                        secondPayload),
                    ignored =>
                    {
                        staleHandlerCalls++;
                        return RelayLiveLoopResult.Success();
                    },
                    CancellationToken.None);

                dispatcher.DrainOnce();
                True(first.GetAwaiter().GetResult().Succeeded, "completed runtime change publishes a transition");
                Equal(1, runtimeValue, "synthetic runtime mutation executed once");
                Equal("revision-after", identity.RuntimeRevision, "identity reads the shared advanced revision");
                Equal("revision-after", bridge.RuntimeRevisions.CurrentRevision, "bridge exposes the same revision clock");
                Equal("revision-before", receipt.PreviousRevision, "transition records its prior revision");
                Equal("revision-after", receipt.RuntimeRevisionAfter, "transition records caller supplied next revision");
                Equal(
                    RelayLiveLoopErrorCode.StaleTarget,
                    queuedStale.GetAwaiter().GetResult().Error.Code,
                    "queued stale request is rechecked on the main thread");
                Equal(0, staleHandlerCalls, "stale queued handler never runs");
                Equal(0L, bridge.Providers.GetOwnerGeneration("owner-synthetic"), "runtime revision does not invent owner generations");

                var staleHandshake = bridge.Authentication.CompleteHandshake(
                    pendingChallenge.ChallengeId,
                    SessionAuthentication.ComputeHandshakeProof(secret, identity, pendingChallenge));
                Equal(RelayLiveLoopErrorCode.StaleTarget, staleHandshake.Error.Code, "in-flight old-revision challenge becomes stale");
                Equal(
                    RelayLiveLoopErrorCode.StaleTarget,
                    bridge.Authentication.BeginHandshake(
                        identity.SessionId,
                        identity.LaunchId,
                        "revision-before",
                        identity.ProtocolVersion,
                        "old-client").Error.Code,
                    "old revision handshake is stale target");

                var nextChallenge = bridge.Authentication.BeginHandshake(
                    identity.SessionId,
                    identity.LaunchId,
                    "revision-after",
                    identity.ProtocolVersion,
                    "next-client");
                True(nextChallenge.Succeeded, "legitimate next revision handshake is accepted");
                True(
                    bridge.Authentication.CompleteHandshake(
                        nextChallenge.Value.ChallengeId,
                        SessionAuthentication.ComputeHandshakeProof(secret, identity, nextChallenge.Value)).Succeeded,
                    "legitimate next revision handshake completes");

                var thirdPayload = Encoding.UTF8.GetBytes("{\"step\":3}");
                var thirdCalls = 0;
                var third = bridge.ScheduleAuthenticated(
                    new AuthenticatedRuntimeRequest(
                        identity.SessionId,
                        "revision-after",
                        SignedRequest(key, connection.ConnectionId, 3, now, "after-request", "observe", thirdPayload),
                        thirdPayload),
                    ignored =>
                    {
                        thirdCalls++;
                        return RelayLiveLoopResult.Success();
                    },
                    CancellationToken.None);
                dispatcher.DrainOnce();
                True(third.GetAwaiter().GetResult().Succeeded, "existing authenticated connection accepts next revision");
                Equal(1, thirdCalls, "next revision handler executes");

                var gameObject = new GameObject("observed", typeof(RectTransform));
                var gameObjectHandle = bridge.ObjectHandles.Register("view-synthetic", gameObject);
                var observation = new MainThreadObservationService(identity, dispatcher, bridge.ObjectHandles);
                var observed = observation.ObserveObject(gameObjectHandle, 8);
                Equal("revision-after", observed.Value.RuntimeRevision, "object observation reads the shared revision");
                var property = observation.ObserveProperty(observed.Value.Components[0].Handle, "anchoredPosition");
                Equal("revision-after", property.Value.RuntimeRevision, "property observation reads the shared revision");

                var capture = new FreshFrameCaptureService();
                capture.InitializeOnMainThread(identity, dispatcher, artifactRoot, 4096);
                var staleCapture = capture.Capture(new FreshFrameCaptureRequest
                {
                    ArtifactId = "stale-frame",
                    ExpectedSessionId = identity.SessionId,
                    ExpectedRuntimeRevision = "revision-before",
                    MinimumFrameExclusive = 200,
                    MaximumWidth = 64,
                    MaximumHeight = 64,
                    Timeout = TimeSpan.FromSeconds(2)
                }, CancellationToken.None).GetAwaiter().GetResult();
                Equal(RelayLiveLoopErrorCode.StaleTarget, staleCapture.Error.Code, "capture maps revision mismatch to stale target");

                var captured = capture.Capture(new FreshFrameCaptureRequest
                {
                    ArtifactId = "current-frame",
                    ExpectedSessionId = identity.SessionId,
                    ExpectedRuntimeRevision = "revision-after",
                    MinimumFrameExclusive = 200,
                    MaximumWidth = 64,
                    MaximumHeight = 64,
                    Timeout = TimeSpan.FromSeconds(2)
                }, CancellationToken.None).GetAwaiter().GetResult();
                True(captured.Succeeded, "capture accepts the current revision");
                Equal("revision-after", captured.Value.RuntimeRevision, "capture artifact is labeled with the captured revision");

                Equal(
                    RelayLiveLoopErrorCode.StaleTarget,
                    bridge.PublishAuthorizedCompletedRuntimeTransition(
                        "stale-transition",
                        "revision-before",
                        "revision-later").Error.Code,
                    "stale transition is rejected");
                Equal(
                    RelayLiveLoopErrorCode.ContractMismatch,
                    bridge.PublishAuthorizedCompletedRuntimeTransition(
                        "unchanged-transition",
                        "revision-after",
                        "revision-after").Error.Code,
                    "transition must use a distinct caller supplied revision");

                var offThreadRejected = Task.Run(() =>
                {
                    try
                    {
                        bridge.PublishAuthorizedCompletedRuntimeTransition(
                            "off-thread-transition",
                            "revision-after",
                            "revision-off-thread");
                        return false;
                    }
                    catch (InvalidOperationException)
                    {
                        return true;
                    }
                }).GetAwaiter().GetResult();
                True(offThreadRejected, "runtime revision transition is main-thread owned");
                Equal("revision-after", identity.RuntimeRevision, "rejected transitions do not mutate the clock");

                Throws<ArgumentException>(() =>
                    new ProviderReply<PageRuntimeState>(
                        new PageRuntimeState(),
                        true,
                        Array.Empty<ProviderEvidence>()),
                    "provider boolean alone cannot claim a runtime change");
                Equal("revision-after", identity.RuntimeRevision, "provider boolean cannot invent a revision");
                var reply = new ProviderReply<PageRuntimeState>(
                    new PageRuntimeState(),
                    receipt,
                    Array.Empty<ProviderEvidence>());
                True(reply.RuntimeChanged == true, "transition receipt reports a known runtime change");
                Equal("revision-after", reply.RuntimeRevisionAfter, "provider reply publishes receipt revision");
                Equal("change-request", reply.RuntimeTransitionId, "provider reply preserves transition operation id");

                var providerHub = new RuntimeProviderHub(
                    dispatcher,
                    bridge.Providers,
                    bridge.RuntimeRevisions);
                bridge.Providers.Register<IPageProvider>(
                    RelayLiveLoopProviderKind.Page,
                    "receipt-page",
                    "receipt-owner",
                    0,
                    new ReceiptPageProvider(bridge));
                var routed = providerHub.ObservePage(new ProviderAddress
                {
                    Kind = RelayLiveLoopProviderKind.Page,
                    ProviderId = "receipt-page",
                    OwnerId = "receipt-owner",
                    OwnerGeneration = 0
                }, new PageOperationRequest
                {
                    TaskId = "task-receipt",
                    PageId = "receipt-page",
                    ExpectedViewGeneration = 0
                });
                True(routed.Succeeded, "provider hub preserves an authoritative transition receipt");
                Equal("revision-provider", routed.Value.RuntimeRevisionAfter, "routed provider reply preserves next revision");
                Equal(
                    RelayLiveLoopErrorCode.ContractMismatch,
                    bridge.PublishAuthorizedCompletedRuntimeTransition(
                        "reused-revision",
                        "revision-provider",
                        "revision-before").Error.Code,
                    "a prior revision cannot be reused to defeat stale-target checks");

                bridge.Providers.Register<IPageProvider>(
                    RelayLiveLoopProviderKind.Page,
                    "flag-page",
                    "flag-owner",
                    0,
                    new FlagOnlyPageProvider());
                var flagOnly = providerHub.ObservePage(new ProviderAddress
                {
                    Kind = RelayLiveLoopProviderKind.Page,
                    ProviderId = "flag-page",
                    OwnerId = "flag-owner",
                    OwnerGeneration = 0
                }, new PageOperationRequest
                {
                    TaskId = "task-flag",
                    PageId = "flag-page",
                    ExpectedViewGeneration = 0
                });
                Equal(RelayLiveLoopErrorCode.InternalError, flagOnly.Error.Code, "flag-only provider claim is rejected");
                Equal("revision-provider", identity.RuntimeRevision, "flag-only provider route cannot advance the clock");
            }
        }
        finally
        {
            if (Directory.Exists(artifactRoot)) Directory.Delete(artifactRoot, true);
        }
    }

    private static void TestEditorProviderFailureClassification()
    {
        var provider = new ThrowingEditorProvider();
        var hub = new NeutralEditorProviderHub("editor-hub-synthetic");
        hub.RegisterCompile(provider);
        hub.RegisterAsset(provider);
        hub.RegisterSource(provider);

        var malformed = Poll(hub, "source.edit", "{");
        Equal("CONTRACT_MISMATCH", malformed.Error.code, "malformed JSON remains a contract mismatch");
        Equal("source.edit", malformed.Error.stage, "malformed JSON retains operation stage");
        True(malformed.Error.runtimeChangedKnown && !malformed.Error.runtimeChanged, "malformed JSON never entered provider");

        var invalidShape = Poll(hub, "source.edit", JsonUtility.ToJson(new SourceJobPayload
        {
            sourceGuid = string.Empty,
            localId = 1,
            propertyPath = "value",
            expectedValueJson = "1",
            replacementValueJson = "2"
        }));
        Equal("CONTRACT_MISMATCH", invalidShape.Error.code, "invalid payload shape remains a contract mismatch");
        Equal(0, provider.SourceEditCalls, "shape failure does not enter source provider");

        var compile = Poll(hub, "compile", JsonUtility.ToJson(new CompileJobPayload
        {
            buildTarget = "SyntheticTarget",
            configuration = "Development",
            sourceInputs = new List<string> { "Synthetic.cs" }
        }));
        Equal("COMPILE_FAILED", compile.Error.code, "compile provider exception uses operation failure code");
        Equal("compile", compile.Error.stage, "compile provider exception retains stage");

        var asset = Poll(hub, "asset.build", JsonUtility.ToJson(new AssetBuildJobPayload
        {
            packageId = "package-synthetic",
            buildProfileId = "profile-synthetic",
            affectedAssetIds = new List<string> { "asset-synthetic" }
        }));
        Equal("RESOURCE_BUILD_FAILED", asset.Error.code, "asset provider exception uses operation failure code");
        Equal("asset.build", asset.Error.stage, "asset provider exception retains stage");

        var locatePayload = ValidSourcePayload();
        var locate = Poll(hub, "source.locate", JsonUtility.ToJson(locatePayload));
        Equal("INTERNAL_ERROR", locate.Error.code, "read-only locate exception is not a contract mismatch");
        Equal("source.locate", locate.Error.stage, "locate provider exception retains stage");

        var edit = Poll(hub, "source.edit", JsonUtility.ToJson(locatePayload));
        Equal("STATE_UNKNOWN", edit.Error.code, "ambiguous source edit exception is state unknown");
        Equal("source.edit", edit.Error.stage, "source edit exception retains stage");
        True(!edit.Error.runtimeChangedKnown, "ambiguous source edit does not invent mutation truth");
        Equal(1, provider.SourceEditCalls, "valid source edit entered provider exactly once");

        var pollingHub = new NeutralEditorProviderHub("editor-hub-polling");
        pollingHub.RegisterSource(new PollThrowingSourceProvider());
        var pollException = Poll(pollingHub, "source.edit", JsonUtility.ToJson(ValidSourcePayload()));
        Equal("STATE_UNKNOWN", pollException.Error.code, "source edit poll exception remains state unknown");
        Equal("source.edit", pollException.Error.stage, "source edit poll exception retains stage");
        True(!pollException.Error.runtimeChangedKnown, "source edit poll exception preserves unknown mutation state");
    }

    private static void TestDurableSourceEditUnknownDoesNotReplay()
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            "relay-liveloop-editor-recovery-" + Guid.NewGuid().ToString("N"));
        try
        {
            var jobs = Path.Combine(root, "jobs");
            var artifacts = Path.Combine(root, "artifacts");
            var store = new AtomicEditorJobStore(jobs, artifacts);
            var provider = new ThrowingEditorProvider();
            var hub = new NeutralEditorProviderHub("editor-hub-durable");
            hub.RegisterSource(provider);
            var providers = new EditorJobProviderRegistry();
            providers.Register(hub);
            var request = Request("source.edit", JsonUtility.ToJson(ValidSourcePayload()));
            request.providerId = hub.ProviderId;
            request.artifactRoot = artifacts;
            File.WriteAllText(
                Path.Combine(store.IncomingDirectory, request.jobId + ".request.json"),
                JsonUtility.ToJson(request, true));

            var worker = new RelayLiveLoopEditorWorker(store, providers, TimeSpan.FromMilliseconds(10));
            PumpUntilResult(worker, jobs, request.jobId);
            var result = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(
                Path.Combine(jobs, "results", request.jobId + ".result.json")));
            Equal("state_unknown", result.status, "ambiguous source edit is durably state_unknown");
            Equal("STATE_UNKNOWN", result.error.code, "durable source edit retains unknown code");
            Equal("source.edit", result.error.stage, "durable source edit retains operation stage");
            Equal(1, provider.SourceEditCalls, "durable source edit provider entered once");

            var restartedWorker = new RelayLiveLoopEditorWorker(store, providers, TimeSpan.FromMilliseconds(10));
            restartedWorker.Tick();
            Equal(1, provider.SourceEditCalls, "published state_unknown result is not replayed");
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    private static void TestPreviewConflictReportsNoWriteAndExternalDrift()
    {
        Time.Reset(400);
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var identity = new RuntimeSessionIdentity("session-preview", "launch-preview", "revision-preview", 1);
        var handles = new ObjectHandleRegistry(identity, dispatcher);
        var gameObject = new GameObject("preview-target", typeof(RectTransform));
        var gameObjectHandle = handles.Register("view-preview", gameObject);
        var observation = new MainThreadObservationService(identity, dispatcher, handles);
        var previewService = new MainThreadComponentPreviewService(dispatcher, handles);
        var componentHandle = observation.ObserveObject(gameObjectHandle, 8).Value.Components[0].Handle;
        var rect = (RectTransform)gameObject.transform;
        var preview = previewService.Preview(new ComponentPreviewRequest
        {
            TaskId = "task-preview",
            Component = componentHandle,
            Property = "anchoredPosition",
            Expected = ComponentValue.Vector2(0, 0),
            Replacement = ComponentValue.Vector2(5, 6)
        }).Value;

        rect.anchoredPosition = new Vector2(7, 8);
        var reverted = previewService.Revert("task-preview", preview.OverlayId);
        Equal(RelayLiveLoopErrorCode.InputChanged, reverted.Error.Code, "external drift refuses revert");
        True(reverted.Error.RuntimeChanged == false, "refused revert reports no command write");
        Equal("true", reverted.Error.Details["externalDrift"], "external drift is represented separately");
        Equal("vector2:5,6", reverted.Error.Details["expectedAppliedValue"], "drift details preserve expected applied value");
        Equal("vector2:7,8", reverted.Error.Details["observedCurrentValue"], "drift details preserve observed value");
        Equal(7f, rect.anchoredPosition.x, "refused revert leaves external x value untouched");
        Equal(8f, rect.anchoredPosition.y, "refused revert leaves external y value untouched");
    }

    private static EditorJobPollResult Poll(NeutralEditorProviderHub hub, string kind, string payloadJson)
    {
        var request = Request(kind, payloadJson);
        request.providerId = hub.ProviderId;
        var attempt = new EditorJobAttemptRecord
        {
            jobId = request.jobId,
            providerId = request.providerId,
            inputSnapshot = request.inputSnapshot,
            requestDigest = new string('a', 64),
            attemptId = "attempt-synthetic",
            attemptRoot = "artifact-synthetic",
            state = "started"
        };
        return hub.Begin(new EditorJobExecution(request, attempt, false)).Poll(TimeSpan.FromMilliseconds(1));
    }

    private static EditorJobRequest Request(string kind, string payloadJson)
    {
        return new EditorJobRequest
        {
            jobId = "job-" + Guid.NewGuid().ToString("N"),
            kind = kind,
            inputSnapshot = "snapshot-synthetic",
            providerId = "editor-hub-synthetic",
            artifactRoot = "artifact-synthetic",
            requestedAtUtc = DateTimeOffset.UtcNow.ToString("o"),
            expiresAtUtc = DateTimeOffset.UtcNow.AddMinutes(2).ToString("o"),
            payloadJson = payloadJson
        };
    }

    private static SourceJobPayload ValidSourcePayload()
    {
        return new SourceJobPayload
        {
            sourceGuid = "source-synthetic",
            localId = 1,
            propertyPath = "value",
            expectedValueJson = "1",
            replacementValueJson = "2"
        };
    }

    private static void PumpUntilResult(RelayLiveLoopEditorWorker worker, string jobRoot, string jobId)
    {
        var resultPath = Path.Combine(jobRoot, "results", jobId + ".result.json");
        for (var index = 0; index < 200 && !File.Exists(resultPath); index++)
        {
            worker.Tick();
            Thread.Sleep(2);
        }
        True(File.Exists(resultPath), "editor result published");
    }

    private static RequestAuthentication SignedRequest(
        byte[] key,
        string connectionId,
        long sequence,
        DateTimeOffset now,
        string requestId,
        string operation,
        byte[] payload)
    {
        var unsigned = new RequestAuthentication(
            connectionId,
            sequence,
            now.ToUnixTimeMilliseconds(),
            requestId,
            operation,
            SessionAuthentication.ComputeSha256(payload),
            string.Empty);
        return new RequestAuthentication(
            unsigned.ConnectionId,
            unsigned.Sequence,
            unsigned.SentAtUnixMilliseconds,
            unsigned.RequestId,
            unsigned.Operation,
            unsigned.PayloadSha256,
            SessionAuthentication.ComputeRequestProof(key, unsigned));
    }

    private static byte[] Secret()
    {
        var secret = new byte[32];
        for (var index = 0; index < secret.Length; index++) secret[index] = (byte)(index + 1);
        return secret;
    }

    private static void True(bool condition, string label)
    {
        if (!condition) throw new InvalidOperationException("Assertion failed: " + label);
        _passed++;
    }

    private static void Equal<T>(T expected, T actual, string label)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
        {
            throw new InvalidOperationException(
                "Assertion failed: " + label + "; expected=" + expected + "; actual=" + actual);
        }
        _passed++;
    }

    private static void Throws<T>(Action action, string label) where T : Exception
    {
        try
        {
            action();
        }
        catch (T)
        {
            _passed++;
            return;
        }
        throw new InvalidOperationException("Assertion failed: " + label);
    }

    private sealed class ThrowingEditorProvider :
        ICompileJobProvider,
        IAssetBuildJobProvider,
        ISourceJobProvider
    {
        public int SourceEditCalls { get; private set; }

        public IEditorJobOperation BeginCompile(CompileJobPayload payload, EditorJobExecution execution)
        {
            throw new InvalidOperationException("synthetic compile provider failure");
        }

        public IEditorJobOperation RecoverCompile(CompileJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }

        public IEditorJobOperation BeginAssetBuild(AssetBuildJobPayload payload, EditorJobExecution execution)
        {
            throw new InvalidOperationException("synthetic asset provider failure");
        }

        public IEditorJobOperation RecoverAssetBuild(AssetBuildJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }

        public IEditorJobOperation BeginLocate(SourceJobPayload payload, EditorJobExecution execution)
        {
            throw new InvalidOperationException("synthetic locate provider failure");
        }

        public IEditorJobOperation RecoverLocate(SourceJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }

        public IEditorJobOperation BeginEdit(SourceJobPayload payload, EditorJobExecution execution)
        {
            SourceEditCalls++;
            throw new InvalidOperationException("synthetic edit failure after unknown provider state");
        }

        public IEditorJobOperation RecoverEdit(SourceJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }
    }

    private sealed class ReceiptPageProvider : IPageProvider
    {
        private readonly RuntimeBridgeCore _bridge;

        public ReceiptPageProvider(RuntimeBridgeCore bridge)
        {
            _bridge = bridge;
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Observe(PageOperationRequest request)
        {
            var transition = _bridge.PublishAuthorizedCompletedRuntimeTransition(
                "provider-change",
                "revision-after",
                "revision-provider");
            if (!transition.Succeeded)
            {
                return RelayLiveLoopResult<ProviderReply<PageRuntimeState>>.Failure(transition.Error);
            }

            return RelayLiveLoopResult<ProviderReply<PageRuntimeState>>.Success(
                new ProviderReply<PageRuntimeState>(
                    new PageRuntimeState(),
                    transition.Value,
                    Array.Empty<ProviderEvidence>()));
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Refresh(PageOperationRequest request)
        {
            return Observe(request);
        }

        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(PageOperationRequest request)
        {
            throw new NotSupportedException();
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RestoreContext(
            PageOperationRequest request,
            SerializedContextEnvelope context)
        {
            return Observe(request);
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Rebuild(PageOperationRequest request)
        {
            return Observe(request);
        }
    }

    private sealed class PollThrowingSourceProvider : ISourceJobProvider
    {
        public IEditorJobOperation BeginLocate(SourceJobPayload payload, EditorJobExecution execution)
        {
            return new PollThrowingOperation();
        }

        public IEditorJobOperation RecoverLocate(SourceJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }

        public IEditorJobOperation BeginEdit(SourceJobPayload payload, EditorJobExecution execution)
        {
            return new PollThrowingOperation();
        }

        public IEditorJobOperation RecoverEdit(SourceJobPayload payload, EditorJobExecution execution)
        {
            return null;
        }
    }

    private sealed class PollThrowingOperation : IEditorJobOperation
    {
        public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
        {
            throw new InvalidOperationException("synthetic poll failure after unknown provider state");
        }
    }

    private sealed class FlagOnlyPageProvider : IPageProvider
    {
        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Observe(PageOperationRequest request)
        {
            return RelayLiveLoopResult<ProviderReply<PageRuntimeState>>.Success(
                new ProviderReply<PageRuntimeState>(
                    new PageRuntimeState(),
                    true,
                    Array.Empty<ProviderEvidence>()));
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Refresh(PageOperationRequest request)
        {
            return Observe(request);
        }

        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(PageOperationRequest request)
        {
            throw new NotSupportedException();
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RestoreContext(
            PageOperationRequest request,
            SerializedContextEnvelope context)
        {
            return Observe(request);
        }

        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Rebuild(PageOperationRequest request)
        {
            return Observe(request);
        }
    }
}
#endif
