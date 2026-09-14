#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using RelayLiveLoop;
using UnityEngine;

internal static class SyntheticTests
{
    private static int _passed;

    public static int Main()
    {
        try
        {
            TestAuthenticationAndReplayDefense();
            TestBoundedFraming();
            TestMainThreadGenerationsAndIdempotency();
            TestSerializedContextSurfaceIsPlainData();
            TestEditorWorkerCompletesAndSealsCurrentAttempt();
            TestEditorWorkerRefusesUnknownRecovery();
            TestArtifactCannotEscapeAttemptRoot();
            RunLocalMicrobenchmarks();
            Console.WriteLine("SYNTHETIC PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SYNTHETIC FAIL: " + ex);
            return 1;
        }
    }

    private static void TestSerializedContextSurfaceIsPlainData()
    {
        var forbidden = new HashSet<Type>
        {
            typeof(Type),
            typeof(Delegate),
            typeof(System.Threading.Tasks.Task),
            typeof(CancellationToken),
            typeof(UnityEngine.Object)
        };
        var properties = typeof(SerializedContextEnvelope).GetProperties();
        for (var index = 0; index < properties.Length; index++)
        {
            True(!forbidden.Contains(properties[index].PropertyType), "serialized context property is plain data");
        }
    }

    private static void TestAuthenticationAndReplayDefense()
    {
        var now = DateTimeOffset.Parse("2026-01-02T03:04:05Z");
        var secret = Secret();
        var identity = new RuntimeSessionIdentity("session-a", "launch-a", "revision-a", 1);
        using (var authentication = new SessionAuthentication(identity, secret, () => now))
        {
            var wrong = authentication.BeginHandshake("session-b", "launch-a", "revision-a", 1, "client-a");
            Equal(RelayLiveLoopErrorCode.WrongSession, wrong.Error.Code, "wrong session rejected");

            var begun = authentication.BeginHandshake("session-a", "launch-a", "revision-a", 1, "client-a");
            True(begun.Succeeded, "handshake challenge issued");
            var key = SessionAuthentication.DeriveConnectionKey(secret, identity, begun.Value);
            var proof = SessionAuthentication.ComputeHandshakeProof(secret, identity, begun.Value);
            var completed = authentication.CompleteHandshake(begun.Value.ChallengeId, proof);
            True(completed.Succeeded, "handshake proof accepted");

            var request = SignedRequest(key, completed.Value.ConnectionId, 1, now, "request-a", "observe", Encoding.UTF8.GetBytes("{}"));
            True(authentication.AuthenticateRequest(request).Succeeded, "request proof accepted");
            Equal(
                RelayLiveLoopErrorCode.AuthRequired,
                authentication.AuthenticateRequest(request).Error.Code,
                "request sequence replay rejected");
        }
    }

    private static void TestBoundedFraming()
    {
        var framer = new BoundedMessageFramer(256);
        var payload = Encoding.UTF8.GetBytes("synthetic-message");
        using (var stream = new MemoryStream())
        {
            framer.WriteAsync(stream, payload, TimeSpan.FromSeconds(1), CancellationToken.None).GetAwaiter().GetResult();
            stream.Position = 0;
            var read = framer.ReadAsync(stream, TimeSpan.FromSeconds(1), CancellationToken.None).GetAwaiter().GetResult();
            Equal("synthetic-message", Encoding.UTF8.GetString(read), "bounded frame round-trip");
        }

        Throws<RelayLiveLoopProtocolException>(() =>
            framer.WriteAsync(new MemoryStream(), new byte[257], TimeSpan.FromSeconds(1), CancellationToken.None)
                .GetAwaiter().GetResult(), "oversized frame rejected");
    }

    private static void TestMainThreadGenerationsAndIdempotency()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread(8, 8, 20);
        var identity = new RuntimeSessionIdentity("session-c", "launch-c", "revision-c", 1);
        var handles = new ObjectHandleRegistry(identity, dispatcher);
        var target = new SyntheticUnityObject();
        var handle = handles.Register("view-a", target);
        True(handles.Resolve<SyntheticUnityObject>(handle).Succeeded, "current object handle resolves");
        handles.AdvanceGeneration("view-a");
        Equal(RelayLiveLoopErrorCode.StaleTarget, handles.Resolve<SyntheticUnityObject>(handle).Error.Code, "old object generation rejected");

        var providers = new ProviderRegistry(dispatcher);
        var provider = new SyntheticProvider();
        providers.Register(RelayLiveLoopProviderKind.Page, "page", "module-a", 0, provider);
        True(providers.Resolve<SyntheticProvider>(RelayLiveLoopProviderKind.Page, "page", "module-a", 0).Succeeded, "provider resolves in current generation");
        providers.AdvanceOwnerGeneration("module-a");
        Equal(
            RelayLiveLoopErrorCode.StaleTarget,
            providers.Resolve<SyntheticProvider>(RelayLiveLoopProviderKind.Page, "page", "module-a", 0).Error.Code,
            "old provider generation rejected");

        var secret = Secret();
        using (var bridge = new RuntimeBridgeCore(identity, secret, dispatcher, new RuntimeBridgeLimits(1024, 8, TimeSpan.FromSeconds(2))))
        {
            var now = DateTimeOffset.UtcNow;
            var challenge = bridge.Authentication.BeginHandshake("session-c", "launch-c", "revision-c", 1, "client-c").Value;
            var key = SessionAuthentication.DeriveConnectionKey(secret, identity, challenge);
            var connection = bridge.Authentication.CompleteHandshake(
                challenge.ChallengeId,
                SessionAuthentication.ComputeHandshakeProof(secret, identity, challenge)).Value;
            var payload = Encoding.UTF8.GetBytes("{}");
            var auth1 = SignedRequest(key, connection.ConnectionId, 1, now, "same-request", "observe", payload);
            var auth2 = SignedRequest(key, connection.ConnectionId, 2, now, "same-request", "observe", payload);
            var calls = 0;
            var first = bridge.ScheduleAuthenticated(
                new AuthenticatedRuntimeRequest("session-c", "revision-c", auth1, payload),
                ignored => { calls++; return RelayLiveLoopResult.Success(); },
                CancellationToken.None);
            var duplicate = bridge.ScheduleAuthenticated(
                new AuthenticatedRuntimeRequest("session-c", "revision-c", auth2, payload),
                ignored => { calls++; return RelayLiveLoopResult.Success(); },
                CancellationToken.None);
            True(object.ReferenceEquals(first, duplicate), "duplicate request shares tracked result");
            dispatcher.DrainOnce();
            True(first.GetAwaiter().GetResult().Succeeded, "authenticated request executed");
            Equal(1, calls, "duplicate request executed once");
        }
    }

    private static void TestEditorWorkerCompletesAndSealsCurrentAttempt()
    {
        WithTemporaryRoots((jobRoot, artifactRoot) =>
        {
            var store = new AtomicEditorJobStore(jobRoot, artifactRoot);
            var providers = new EditorJobProviderRegistry();
            providers.Register(new ImmediateArtifactProvider("provider-a", false));
            WriteRequest(store.IncomingDirectory, "job-a", "provider-a", "build", artifactRoot);
            var worker = new RelayLiveLoopEditorWorker(store, providers, TimeSpan.FromMilliseconds(10));
            PumpUntilResult(worker, jobRoot, "job-a");
            var result = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(Path.Combine(jobRoot, "results", "job-a.result.json")));
            Equal("completed", result.status, "editor job completed");
            Equal(1, result.artifacts.Count, "editor artifact listed");
            Equal(64, result.artifacts[0].sha256.Length, "editor artifact sealed with sha256");
            True(result.artifacts[0].path.StartsWith(artifactRoot, StringComparison.OrdinalIgnoreCase), "artifact stayed in configured root");
        });
    }

    private static void TestEditorWorkerRefusesUnknownRecovery()
    {
        WithTemporaryRoots((jobRoot, artifactRoot) =>
        {
            var store = new AtomicEditorJobStore(jobRoot, artifactRoot);
            var providers = new EditorJobProviderRegistry();
            providers.Register(new UnrecoverableProvider());
            WriteRequest(store.IncomingDirectory, "job-b", "provider-b", "source", artifactRoot);
            var firstWorker = new RelayLiveLoopEditorWorker(store, providers);
            firstWorker.Tick();
            var recoveredWorker = new RelayLiveLoopEditorWorker(store, providers);
            recoveredWorker.Tick();
            var result = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(Path.Combine(jobRoot, "results", "job-b.result.json")));
            Equal("state_unknown", result.status, "unreconciled domain reload marked state unknown");
            Equal("STATE_UNKNOWN", result.error.code, "unreconciled attempt not replayed");
        });
    }

    private static void TestArtifactCannotEscapeAttemptRoot()
    {
        WithTemporaryRoots((jobRoot, artifactRoot) =>
        {
            var store = new AtomicEditorJobStore(jobRoot, artifactRoot);
            var providers = new EditorJobProviderRegistry();
            providers.Register(new ImmediateArtifactProvider("provider-c", true));
            WriteRequest(store.IncomingDirectory, "job-c", "provider-c", "build", artifactRoot);
            var worker = new RelayLiveLoopEditorWorker(store, providers);
            PumpUntilResult(worker, jobRoot, "job-c");
            var result = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(Path.Combine(jobRoot, "results", "job-c.result.json")));
            Equal("failed", result.status, "artifact path escape failed job");
            Equal("CONTRACT_MISMATCH", result.error.code, "artifact path escape reported contract mismatch");
        });
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

    private static void RunLocalMicrobenchmarks()
    {
        const int iterations = 2000;
        var now = DateTimeOffset.UtcNow;
        var secret = Secret();
        var identity = new RuntimeSessionIdentity("bench-session", "bench-launch", "bench-revision", 1);
        var stopwatch = Stopwatch.StartNew();
        using (var authentication = new SessionAuthentication(identity, secret, () => now, maximumConnections: 1))
        {
            var challenge = authentication.BeginHandshake(
                identity.SessionId,
                identity.LaunchId,
                identity.RuntimeRevision,
                identity.ProtocolVersion,
                "bench-client").Value;
            var key = SessionAuthentication.DeriveConnectionKey(secret, identity, challenge);
            var connection = authentication.CompleteHandshake(
                challenge.ChallengeId,
                SessionAuthentication.ComputeHandshakeProof(secret, identity, challenge)).Value;
            var payload = Encoding.UTF8.GetBytes("{}");
            for (var sequence = 1; sequence <= iterations; sequence++)
            {
                var request = SignedRequest(key, connection.ConnectionId, sequence, now, "bench-" + sequence, "observe", payload);
                if (!authentication.AuthenticateRequest(request).Succeeded) throw new InvalidOperationException("Benchmark request failed.");
            }
        }
        stopwatch.Stop();
        Console.WriteLine("BENCHMARK synthetic authentication requests=" + iterations + " elapsed_ms=" + stopwatch.Elapsed.TotalMilliseconds.ToString("F2"));
    }

    private static byte[] Secret()
    {
        var secret = new byte[32];
        for (var index = 0; index < secret.Length; index++) secret[index] = (byte)(index + 1);
        return secret;
    }

    private static void WriteRequest(
        string incoming,
        string jobId,
        string providerId,
        string kind,
        string artifactRoot)
    {
        var request = new EditorJobRequest
        {
            jobId = jobId,
            providerId = providerId,
            kind = kind,
            inputSnapshot = "snapshot-" + jobId,
            artifactRoot = artifactRoot,
            requestedAtUtc = DateTimeOffset.UtcNow.ToString("o"),
            expiresAtUtc = DateTimeOffset.UtcNow.AddMinutes(2).ToString("o"),
            payloadJson = "{}"
        };
        File.WriteAllText(Path.Combine(incoming, jobId + ".request.json"), JsonUtility.ToJson(request, true));
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

    private static void WithTemporaryRoots(Action<string, string> test)
    {
        var root = Path.Combine(Path.GetTempPath(), "relay-liveloop-synthetic-" + Guid.NewGuid().ToString("N"));
        var jobs = Path.Combine(root, "jobs");
        var artifacts = Path.Combine(root, "artifacts");
        try
        {
            test(jobs, artifacts);
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
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
            throw new InvalidOperationException("Assertion failed: " + label + "; expected=" + expected + "; actual=" + actual);
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

    private sealed class SyntheticUnityObject : UnityEngine.Object
    {
    }

    private sealed class SyntheticProvider
    {
    }

    private sealed class ImmediateArtifactProvider : IEditorJobProvider
    {
        private readonly bool _escapeAttemptRoot;

        public ImmediateArtifactProvider(string providerId, bool escapeAttemptRoot)
        {
            ProviderId = providerId;
            _escapeAttemptRoot = escapeAttemptRoot;
        }

        public string ProviderId { get; private set; }
        public bool Supports(string kind) { return kind == "build"; }

        public IEditorJobOperation Begin(EditorJobExecution execution)
        {
            var path = _escapeAttemptRoot
                ? Path.Combine(execution.Request.artifactRoot, "outside.txt")
                : Path.Combine(execution.Attempt.attemptRoot, "artifact.txt");
            File.WriteAllText(path, "synthetic artifact");
            return new CompleteOperation(path);
        }

        public IEditorJobOperation Recover(EditorJobExecution execution)
        {
            return null;
        }
    }

    private sealed class CompleteOperation : IEditorJobOperation
    {
        private readonly string _path;

        public CompleteOperation(string path) { _path = path; }

        public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
        {
            return EditorJobPollResult.Completed(
                "{\"synthetic\":true}",
                new[]
                {
                    new EditorJobArtifact
                    {
                        artifactId = "artifact-a",
                        kind = "synthetic",
                        path = _path,
                        mediaType = "text/plain"
                    }
                });
        }
    }

    private sealed class UnrecoverableProvider : IEditorJobProvider
    {
        public string ProviderId { get { return "provider-b"; } }
        public bool Supports(string kind) { return kind == "source"; }
        public IEditorJobOperation Begin(EditorJobExecution execution) { return new RunningOperation(); }
        public IEditorJobOperation Recover(EditorJobExecution execution) { return null; }
    }

    private sealed class RunningOperation : IEditorJobOperation
    {
        public EditorJobPollResult Poll(TimeSpan mainThreadBudget) { return EditorJobPollResult.Running(); }
    }
}
#endif
