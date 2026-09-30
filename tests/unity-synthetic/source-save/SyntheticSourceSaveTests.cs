#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using RelayLiveLoop;
using UnityEngine;

internal static class SyntheticSourceSaveTests
{
    private static int _checks;
    public static int Main(string[] args)
    {
        try
        {
            var root = args[0];
            VerifySaveAndRecovery(Path.Combine(root, "saved"));
            VerifyFailure(Path.Combine(root, "before-write"), "before", "failed", true);
            VerifyFailure(Path.Combine(root, "after-write"), "after", "state_unknown", false);
            VerifyFailure(Path.Combine(root, "readback"), "readback", "failed", true);
            VerifyDirtyAndStale(Path.Combine(root, "dirty"), true, false);
            VerifyDirtyAndStale(Path.Combine(root, "stale"), false, true);
            VerifyUnknownRecovery(Path.Combine(root, "unknown"));
            Console.WriteLine("SOURCE SAVE SYNTHETIC PASS: " + _checks + " assertions; no Unity Editor or Player was launched.");
            return 0;
        }
        catch (Exception exception) { Console.Error.WriteLine(exception); return 1; }
    }

    private static EditorJobExecution Execution(string root)
    {
        Directory.CreateDirectory(root);
        return new EditorJobExecution(new EditorJobRequest { jobId = "job-source-synthetic", providerId = "provider-synthetic" },
            new EditorJobAttemptRecord { jobId = "job-source-synthetic", requestDigest = new string('b', 64),
                attemptId = "attempt-synthetic", attemptRoot = root }, false);
    }

    private static SourceEditContext Context(Backend backend)
    {
        return new SourceEditContext { schema = "relay.liveloop.source-edit-context", version = 1,
            bindingJson = "{\"taskId\":\"task-synthetic\"}", expectedSourceHash = backend.ReadPersistedHash(),
            edits = new List<SourcePropertyEdit> { new SourcePropertyEdit { propertyPath = "value",
                expectedValueJson = "1", replacementValueJson = "2" } } };
    }

    private static SourceJobPayload Payload() { return new SourceJobPayload { sourceGuid = new string('a', 32), localId = 1, propertyPath = "value" }; }

    private static void VerifySaveAndRecovery(string root)
    {
        var backend = new Backend(); var context = Context(backend); var execution = Execution(root);
        var operation = DurableSourceEdit.CreateOperation(Payload(), context, execution, backend, false);
        var result = operation.Poll(TimeSpan.FromMilliseconds(4));
        var saved = JsonUtility.FromJson<SourceSaveReceipt>(result.ResultJson);
        Equal("completed", saved.phase); True(saved.sourcePersisted); True(saved.sourceChangedKnown); True(saved.sourceChanged);
        True(!saved.runtimeApplied); Equal("2", backend.Persistent); Equal(1, result.Artifacts.Count); True(File.Exists(result.Artifacts[0].path));
        var writes = backend.WriteCalls; var saves = backend.SaveCalls;
        var recovered = DurableSourceEdit.Recover(Payload(), context, execution, backend);
        Equal("completed", recovered.phase); Equal(writes, backend.WriteCalls); Equal(saves, backend.SaveCalls);
        Equal(result, operation.Poll(TimeSpan.FromMilliseconds(4)));
    }

    private static void VerifyFailure(string root, string mode, string phase, bool rollback)
    {
        var backend = new Backend { Mode = mode }; var context = Context(backend); var execution = Execution(root);
        var receipt = DurableSourceEdit.Begin(Payload(), context, execution, backend);
        Equal(phase, receipt.phase); True(!receipt.sourcePersisted); True(receipt.rollbackAttempted); Equal(rollback, receipt.rollbackSucceeded);
        Equal(rollback, receipt.sourceChangedKnown); True(!receipt.runtimeApplied);
        if (rollback) { Equal("1", backend.Persistent); Equal("1", backend.Memory); True(!receipt.sourceChanged); }
        else { Equal("2", backend.Persistent); Equal(1, backend.WriteCalls); }
        var writes = backend.WriteCalls; var saves = backend.SaveCalls;
        var recovered = DurableSourceEdit.Recover(Payload(), context, execution, backend);
        Equal(phase, recovered.phase); Equal(writes, backend.WriteCalls); Equal(saves, backend.SaveCalls);
    }

    private static void VerifyDirtyAndStale(string root, bool dirty, bool stale)
    {
        var backend = new Backend(); var context = Context(backend);
        if (dirty) backend.Clean = false;
        if (stale) backend.Persistent = "3";
        var receipt = DurableSourceEdit.Begin(Payload(), context, Execution(root), backend);
        Equal("failed", receipt.phase); True(!receipt.sourcePersisted); True(receipt.sourceChangedKnown); True(!receipt.sourceChanged);
        Equal(0, backend.WriteCalls); Equal(0, backend.SaveCalls);
    }

    private static void VerifyUnknownRecovery(string root)
    {
        var backend = new Backend(); var context = Context(backend); var execution = Execution(root);
        var lost = DurableSourceEdit.Recover(Payload(), context, execution, backend);
        Equal("state_unknown", lost.phase); Equal(0, backend.WriteCalls); Equal(0, backend.SaveCalls);
        var receipt = DurableSourceEdit.Begin(Payload(), context, execution, backend);
        receipt.phase = "saving";
        File.WriteAllText(Path.Combine(root, "source-edit.journal.json"), JsonUtility.ToJson(receipt));
        var writes = backend.WriteCalls; var saves = backend.SaveCalls;
        var recovered = DurableSourceEdit.Recover(Payload(), context, execution, backend);
        Equal("state_unknown", recovered.phase); Equal(writes, backend.WriteCalls); Equal(saves, backend.SaveCalls);
        receipt.phase = "completed"; receipt.bindingJson = "{\"taskId\":\"other-task\"}";
        File.WriteAllText(Path.Combine(root, "source-edit.journal.json"), JsonUtility.ToJson(receipt));
        Equal("state_unknown", DurableSourceEdit.Recover(Payload(), context, execution, backend).phase);
    }

    private sealed class Backend : ISourceEditBackend
    {
        public string Memory = "1"; public string Persistent = "1"; public string Mode; public bool Clean = true;
        public int WriteCalls; public int SaveCalls;
        public bool IsTargetClean { get { return Clean; } }
        public void ValidateBinding(SourceJobPayload payload, SourceEditContext context) { }
        public string ReadPersistedHash()
        {
            using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Persistent))).Replace("-", "").ToLowerInvariant();
        }
        public string ReadProperty(string propertyPath) { return Memory; }
        public string ReadPersistedProperty(string propertyPath) { return Persistent; }
        public void WriteProperty(string propertyPath, string value) { WriteCalls++; Memory = value; }
        public string SaveTarget()
        {
            SaveCalls++;
            if (SaveCalls == 1 && Mode == "before") throw new IOException("Synthetic save refusal before file write.");
            Persistent = SaveCalls == 1 && Mode == "readback" ? "3" : Memory;
            if (SaveCalls == 1 && Mode == "after") throw new IOException("Synthetic response loss after file write.");
            return ReadPersistedHash();
        }
    }
    private static void True(bool value) { _checks++; if (!value) throw new InvalidOperationException("Assertion failed."); }
    private static void Equal<T>(T expected, T actual) { _checks++; if (!EqualityComparer<T>.Default.Equals(expected, actual)) throw new InvalidOperationException("Expected " + expected + ", got " + actual); }
}
#endif
