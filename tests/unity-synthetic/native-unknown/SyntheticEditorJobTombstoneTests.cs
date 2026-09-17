#if UNITY_EDITOR && RELAYLIVELOOP_EDITOR_STORE_SYNTHETIC
using System;
using System.Globalization;
using System.IO;
using System.Text;
using RelayLiveLoop;
using UnityEngine;

internal static class SyntheticEditorJobTombstoneTests
{
    public static int Main(string[] args)
    {
        try
        {
            if (args == null || args.Length != 1 || string.IsNullOrWhiteSpace(args[0]))
            {
                throw new ArgumentException("One mailbox root is required.");
            }

            VerifyArchivedAttemptCannotBeginAgain(args[0]);
            Console.WriteLine("EDITOR MAILBOX PASS: archived attempt plus missing result produced STATE_UNKNOWN without a second BeginCompile.");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("EDITOR MAILBOX FAIL: " + exception);
            return 1;
        }
    }

    private static void VerifyArchivedAttemptCannotBeginAgain(string mailboxRoot)
    {
        Directory.CreateDirectory(mailboxRoot);
        var artifactRoot = Path.Combine(mailboxRoot, "artifacts");
        var store = new AtomicEditorJobStore(mailboxRoot, artifactRoot);
        var providers = new EditorJobProviderRegistry();
        var provider = new CountingCompileProvider();
        providers.Register(provider);
        var worker = new RelayLiveLoopEditorWorker(store, providers);
        var request = new EditorJobRequest
        {
            jobId = "job-editor-tombstone-replay",
            kind = "compile",
            inputSnapshot = "sha256:" + new string('a', 64),
            providerId = provider.ProviderId,
            artifactRoot = artifactRoot,
            requestedAtUtc = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            expiresAtUtc = DateTimeOffset.UtcNow.AddHours(1).ToString("o", CultureInfo.InvariantCulture),
            payloadJson = "{}"
        };
        var requestBytes = new UTF8Encoding(false).GetBytes(JsonUtility.ToJson(request));
        var incomingPath = Path.Combine(store.IncomingDirectory, request.jobId + ".request.json");
        File.WriteAllBytes(incomingPath, requestBytes);

        worker.Tick();
        Equal(1, provider.BeginCalls, "the first request starts exactly one synthetic compile");
        Equal(0, provider.RecoverCalls, "the first request is not a recovery");
        worker.Tick();

        var resultPath = Path.Combine(mailboxRoot, "results", request.jobId + ".result.json");
        True(File.Exists(resultPath), "first attempt publishes a terminal result");
        var firstResult = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(resultPath, Encoding.UTF8));
        Equal("failed", firstResult.status, "synthetic first attempt archives through the normal terminal path");
        var firstAttemptId = firstResult.attemptId;
        True(Directory.GetFiles(Path.Combine(mailboxRoot, "archive"), request.jobId + ".*.request.json").Length == 1,
            "first request is archived before result loss");
        True(File.Exists(Path.Combine(mailboxRoot, "attempt-tombstones", request.jobId + ".attempt.json")),
            "durable attempt tombstone survives request and attempt archival");

        File.Delete(resultPath);
        File.WriteAllBytes(incomingPath, requestBytes);
        worker.Tick();

        Equal(1, provider.BeginCalls, "same job with missing result never reaches provider Begin a second time");
        Equal(0, provider.RecoverCalls, "archived request is not misclassified as a recoverable active attempt");
        True(!File.Exists(incomingPath), "duplicate incoming request is consumed into a terminal unknown result");
        True(!File.Exists(Path.Combine(mailboxRoot, "processing", request.jobId + ".request.json")),
            "duplicate request leaves no processing request");
        var unknown = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(resultPath, Encoding.UTF8));
        Equal("state_unknown", unknown.status, "duplicate request receives a fail-closed terminal result");
        Equal("STATE_UNKNOWN", unknown.error.code, "duplicate request records the no-replay reason");
        Equal(firstAttemptId, unknown.attemptId, "unknown result points to the prior durable attempt identity");
        True(Directory.GetFiles(Path.Combine(mailboxRoot, "archive"), "*.request.json", SearchOption.AllDirectories).Length >= 2,
            "duplicate mailbox request is preserved in the archive");
    }

    private sealed class CountingCompileProvider : IEditorJobProvider
    {
        public string ProviderId { get { return "synthetic-compile-provider"; } }
        public int BeginCalls { get; private set; }
        public int RecoverCalls { get; private set; }

        public bool Supports(string kind)
        {
            return string.Equals(kind, "compile", StringComparison.Ordinal);
        }

        public IEditorJobOperation Begin(EditorJobExecution execution)
        {
            BeginCalls++;
            return new FailedOperation();
        }

        public IEditorJobOperation Recover(EditorJobExecution execution)
        {
            RecoverCalls++;
            return new FailedOperation();
        }
    }

    private sealed class FailedOperation : IEditorJobOperation
    {
        public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
        {
            return EditorJobPollResult.Failed(AtomicEditorJobStore.Error(
                "INTERNAL_ERROR", "synthetic_compile", "Synthetic compile attempt ended after Begin.", false));
        }
    }

    private static void True(bool value, string message)
    {
        if (!value) throw new InvalidOperationException(message);
    }

    private static void Equal<T>(T expected, T actual, string message)
    {
        if (!object.Equals(expected, actual))
        {
            throw new InvalidOperationException(message + ": expected " + expected + ", got " + actual);
        }
    }
}
#endif
