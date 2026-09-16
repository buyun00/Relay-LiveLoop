#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.IO;
using System.Text;
using RelayLiveLoop;
using UnityEngine;

internal static class EditorResultNullSerializationTests
{
    private static int _passed;

    public static int Main()
    {
        var root = Path.Combine(
            AppDomain.CurrentDomain.BaseDirectory,
            "fixture-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(root);
            var jobs = Path.Combine(root, "jobs");
            var artifacts = Path.Combine(root, "artifacts");
            var store = new AtomicEditorJobStore(jobs, artifacts);

            var failed = PublishFailure(
                store,
                artifacts,
                "job-synthetic-failure",
                "RESOURCE_BUILD_FAILED",
                "asset.build",
                "Synthetic provider failure.");
            True(failed.Contains("\"resultJson\":null"), "failed result serializes resultJson as JSON null");
            True(!failed.Contains("\"resultJson\":\"\""), "failed result does not serialize resultJson as an empty string");
            var failedModel = JsonUtility.FromJson<EditorJobResult>(failed);
            True(failedModel.resultJson == null, "failed result round-trips resultJson as null");
            True(failedModel.status == "failed", "failed result status is preserved");
            True(failedModel.error != null && failedModel.error.code == "RESOURCE_BUILD_FAILED", "failed error code is preserved");
            True(failedModel.error.stage == "asset.build", "failed error stage is preserved");
            True(failedModel.error.message == "Synthetic provider failure.", "failed error message is preserved");
            True(!failedModel.error.recoverable, "failed error recoverable flag is preserved");
            True(!failedModel.error.runtimeChangedKnown, "failed error runtimeChangedKnown flag is preserved");
            True(!failedModel.error.runtimeChanged, "failed error runtimeChanged flag is preserved");

            var unknown = PublishFailure(
                store,
                artifacts,
                "job-synthetic-state-unknown",
                "STATE_UNKNOWN",
                "editor.recovery",
                "Synthetic recovery ambiguity.");
            True(unknown.Contains("\"resultJson\":null"), "state_unknown result serializes resultJson as JSON null");
            True(!unknown.Contains("\"resultJson\":\"\""), "state_unknown result does not serialize resultJson as an empty string");
            var unknownModel = JsonUtility.FromJson<EditorJobResult>(unknown);
            True(unknownModel.status == "state_unknown", "state_unknown status is preserved");
            True(unknownModel.resultJson == null, "state_unknown result round-trips resultJson as null");
            True(unknownModel.error != null && unknownModel.error.code == "STATE_UNKNOWN", "state_unknown error code is preserved");

            var completed = PublishSuccess(store, artifacts, "job-synthetic-completed");
            True(completed.Contains("\"resultJson\":\"\""), "completed empty result remains an empty string for Host rejection");
            True(!completed.Contains("\"resultJson\":null"), "completed empty result is not normalized to null");

            Console.WriteLine("EDITOR RESULT NULL SERIALIZATION PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("EDITOR RESULT NULL SERIALIZATION FAIL: " + exception);
            return 1;
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    private static string PublishFailure(
        AtomicEditorJobStore store,
        string artifacts,
        string jobId,
        string code,
        string stage,
        string message)
    {
        var claim = Claim(store, artifacts, jobId);
        store.BeginAttempt(claim);
        store.PublishFailure(claim, AtomicEditorJobStore.Error(
            code,
            stage,
            message,
            false));
        return File.ReadAllText(Path.Combine(
            Path.GetDirectoryName(store.IncomingDirectory),
            "results",
            jobId + ".result.json"), Encoding.UTF8);
    }

    private static string PublishSuccess(AtomicEditorJobStore store, string artifacts, string jobId)
    {
        var claim = Claim(store, artifacts, jobId);
        store.BeginAttempt(claim);
        store.PublishSuccess(claim, string.Empty, null);
        return File.ReadAllText(Path.Combine(
            Path.GetDirectoryName(store.IncomingDirectory),
            "results",
            jobId + ".result.json"), Encoding.UTF8);
    }

    private static EditorJobClaim Claim(AtomicEditorJobStore store, string artifacts, string jobId)
    {
        var request = new EditorJobRequest
        {
            jobId = jobId,
            kind = "asset.build",
            inputSnapshot = "snapshot-synthetic",
            providerId = "provider-synthetic",
            artifactRoot = artifacts,
            requestedAtUtc = DateTimeOffset.UtcNow.ToString("o"),
            expiresAtUtc = DateTimeOffset.UtcNow.AddMinutes(2).ToString("o"),
            payloadJson = "{}"
        };
        var requestPath = Path.Combine(store.IncomingDirectory, jobId + ".request.json");
        File.WriteAllText(requestPath, JsonUtility.ToJson(request, true), new UTF8Encoding(false));
        var claim = store.TryClaimNext(ignored => true);
        if (claim == null)
            throw new InvalidOperationException("Synthetic request was not claimed.");
        return claim;
    }

    private static void True(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
        _passed++;
    }
}
#endif

