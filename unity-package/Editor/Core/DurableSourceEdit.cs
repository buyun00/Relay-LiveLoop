#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEngine;

namespace RelayLiveLoop
{
    [Serializable]
    public sealed class SourcePropertyEdit
    {
        public string propertyPath;
        public string expectedValueJson;
        public string replacementValueJson;
    }

    [Serializable]
    public sealed class SourceEditContext
    {
        public string schema;
        public int version;
        public string bindingJson;
        public string expectedSourceHash;
        public List<SourcePropertyEdit> edits = new List<SourcePropertyEdit>();
    }

    [Serializable]
    public sealed class SourceSaveReceipt
    {
        public string schema = "relay.liveloop.source-save-receipt";
        public int version = 1;
        public string jobId;
        public string attemptId;
        public string requestDigest;
        public string bindingJson;
        public string phase;
        public string beforeHash;
        public string afterHash;
        public bool sourcePersisted;
        public bool sourceChangedKnown;
        public bool sourceChanged;
        public bool runtimeApplied;
        public bool rollbackAttempted;
        public bool rollbackSucceeded;
        public string errorCode;
        public string errorMessage;
    }

    // Implementations own one clean, explicitly resolved asset. They must never
    // call global SaveAssets, save scenes, or save an unrelated loaded object.
    public interface ISourceEditBackend
    {
        void ValidateBinding(SourceJobPayload payload, SourceEditContext context);
        bool IsTargetClean { get; }
        string ReadPersistedHash();
        string ReadProperty(string propertyPath);
        string ReadPersistedProperty(string propertyPath);
        void WriteProperty(string propertyPath, string canonicalValueJson);
        string SaveTarget();
    }

    /// <summary>Durable target-only edit rules; the private backend owns Unity serialization.</summary>
    public static class DurableSourceEdit
    {
        public static IEditorJobOperation CreateOperation(SourceJobPayload payload, SourceEditContext context,
            EditorJobExecution execution, ISourceEditBackend backend, bool recovering)
        {
            return new SourceOperation(payload, context, execution, backend, recovering);
        }

        public static SourceSaveReceipt Begin(SourceJobPayload payload, SourceEditContext context,
            EditorJobExecution execution, ISourceEditBackend backend)
        {
            Validate(payload, context, execution, backend);
            var path = JournalPath(execution);
            if (File.Exists(path)) return Recover(payload, context, execution, backend);
            var receipt = NewReceipt(context, execution);
            string savedHash = null;
            var modified = false;
            try
            {
                backend.ValidateBinding(payload, context);
                if (!backend.IsTargetClean) throw new InvalidOperationException("The target contains unrelated unsaved changes.");
                if (backend.ReadPersistedHash() != context.expectedSourceHash)
                    throw new InvalidOperationException("The persisted source hash changed before edit.");
                for (var index = 0; index < context.edits.Count; index++)
                {
                    var edit = context.edits[index];
                    if (backend.ReadProperty(edit.propertyPath) != edit.expectedValueJson ||
                        backend.ReadPersistedProperty(edit.propertyPath) != edit.expectedValueJson)
                        throw new InvalidOperationException("The source property's expected value changed.");
                }
                receipt.phase = "prepared";
                Write(path, receipt);
                receipt.phase = "mutating";
                Write(path, receipt);
                modified = true;
                for (var index = 0; index < context.edits.Count; index++)
                    backend.WriteProperty(context.edits[index].propertyPath, context.edits[index].replacementValueJson);
                backend.ValidateBinding(payload, context);
                if (backend.ReadPersistedHash() != context.expectedSourceHash)
                    throw new InvalidOperationException("The persisted source changed during edit; saving is refused.");
                receipt.phase = "saving";
                Write(path, receipt);
                savedHash = backend.SaveTarget();
                if (!IsHash(savedHash) || backend.ReadPersistedHash() != savedHash || !ValuesMatch(context, backend, false))
                    throw new InvalidOperationException("Saved source hash or persisted property readback differs.");
                receipt.afterHash = savedHash;
                receipt.phase = "completed";
                receipt.sourcePersisted = true;
                receipt.sourceChangedKnown = true;
                receipt.sourceChanged = savedHash != receipt.beforeHash;
                Write(path, receipt);
                return receipt;
            }
            catch (Exception failure)
            {
                receipt.errorCode = "CONFLICT";
                receipt.errorMessage = failure.GetType().Name + ": " + failure.Message;
                receipt.phase = "failed";
                receipt.sourcePersisted = false;
                receipt.sourceChanged = false;
                receipt.sourceChangedKnown = !modified;
                if (modified)
                {
                    receipt.rollbackAttempted = true;
                    try
                    {
                        var currentHash = backend.ReadPersistedHash();
                        if (currentHash != receipt.beforeHash && (savedHash == null || currentHash != savedHash))
                            throw new InvalidOperationException("The disk state cannot be attributed to this save; rollback is refused.");
                        receipt.phase = "rolling_back";
                        Write(path, receipt);
                        for (var index = 0; index < context.edits.Count; index++)
                            backend.WriteProperty(context.edits[index].propertyPath, context.edits[index].expectedValueJson);
                        if (currentHash != receipt.beforeHash) backend.SaveTarget();
                        if (backend.ReadPersistedHash() != receipt.beforeHash || !ValuesMatch(context, backend, true))
                            throw new InvalidOperationException("Rollback could not prove the original persistent state.");
                        receipt.afterHash = receipt.beforeHash;
                        receipt.phase = "failed";
                        receipt.rollbackSucceeded = true;
                        receipt.sourceChangedKnown = true;
                    }
                    catch (Exception rollbackFailure)
                    {
                        receipt.phase = "state_unknown";
                        receipt.errorCode = "STATE_UNKNOWN";
                        receipt.errorMessage += "; " + rollbackFailure.GetType().Name + ": " + rollbackFailure.Message;
                        receipt.sourceChangedKnown = false;
                    }
                }
                // A journal write failure must escape. The worker then reports
                // unknown instead of publishing an unrecorded saved fact.
                Write(path, receipt);
                return receipt;
            }
        }

        public static SourceSaveReceipt Recover(SourceJobPayload payload, SourceEditContext context,
            EditorJobExecution execution, ISourceEditBackend backend)
        {
            Validate(payload, context, execution, backend);
            var unknown = NewReceipt(context, execution);
            unknown.phase = "state_unknown";
            unknown.errorCode = "STATE_UNKNOWN";
            unknown.errorMessage = "No terminal matching receipt proves the prior source edit. No write was replayed.";
            var path = JournalPath(execution);
            try
            {
                if (!File.Exists(path) || new FileInfo(path).Length > 1024 * 1024) return unknown;
                var saved = JsonUtility.FromJson<SourceSaveReceipt>(File.ReadAllText(path, Encoding.UTF8));
                if (saved == null || saved.schema != unknown.schema || saved.version != 1 ||
                    saved.jobId != unknown.jobId || saved.attemptId != unknown.attemptId ||
                    saved.requestDigest != unknown.requestDigest || saved.bindingJson != context.bindingJson ||
                    saved.beforeHash != context.expectedSourceHash || saved.runtimeApplied)
                    return unknown;
                backend.ValidateBinding(payload, context);
                if (saved.phase == "completed" && saved.sourcePersisted && saved.sourceChangedKnown &&
                    IsHash(saved.afterHash) && saved.sourceChanged == (saved.beforeHash != saved.afterHash) &&
                    backend.ReadPersistedHash() == saved.afterHash && ValuesMatch(context, backend, false)) return saved;
                if (saved.phase == "failed" && !saved.sourcePersisted && saved.sourceChangedKnown && !saved.sourceChanged &&
                    backend.ReadPersistedHash() == saved.beforeHash && ValuesMatch(context, backend, true)) return saved;
            }
            catch (Exception) { }
            return unknown;
        }

        private static SourceSaveReceipt NewReceipt(SourceEditContext context, EditorJobExecution execution)
        {
            return new SourceSaveReceipt { jobId = execution.Request.jobId, attemptId = execution.Attempt.attemptId,
                requestDigest = execution.Attempt.requestDigest, bindingJson = context.bindingJson,
                beforeHash = context.expectedSourceHash, phase = "prepared", runtimeApplied = false };
        }

        private static bool ValuesMatch(SourceEditContext context, ISourceEditBackend backend, bool original)
        {
            for (var index = 0; index < context.edits.Count; index++)
            {
                var edit = context.edits[index];
                if (backend.ReadPersistedProperty(edit.propertyPath) !=
                    (original ? edit.expectedValueJson : edit.replacementValueJson)) return false;
            }
            return true;
        }

        private static void Validate(SourceJobPayload payload, SourceEditContext context, EditorJobExecution execution, ISourceEditBackend backend)
        {
            if (payload == null || context == null || execution == null || execution.Request == null || execution.Attempt == null || backend == null)
                throw new ArgumentNullException("Source edit inputs are required.");
            if (context.schema != "relay.liveloop.source-edit-context" || context.version != 1 ||
                string.IsNullOrWhiteSpace(context.bindingJson) || context.bindingJson.Length > 64 * 1024 ||
                !IsHash(context.expectedSourceHash) || context.edits == null || context.edits.Count == 0 || context.edits.Count > 128)
                throw new InvalidDataException("Source context fields are invalid.");
            var properties = new HashSet<string>(StringComparer.Ordinal);
            for (var index = 0; index < context.edits.Count; index++)
            {
                var edit = context.edits[index];
                if (edit == null || string.IsNullOrWhiteSpace(edit.propertyPath) || edit.propertyPath.Length > 512 ||
                    !properties.Add(edit.propertyPath) || edit.expectedValueJson == null || edit.replacementValueJson == null)
                    throw new InvalidDataException("Source property edit fields are invalid or duplicated.");
            }
        }

        private static bool IsHash(string value)
        {
            if (value == null || value.Length != 64) return false;
            for (var index = 0; index < value.Length; index++)
                if (!((value[index] >= '0' && value[index] <= '9') || (value[index] >= 'a' && value[index] <= 'f'))) return false;
            return true;
        }

        private static string JournalPath(EditorJobExecution execution)
        {
            return Path.Combine(execution.Attempt.attemptRoot, "source-edit.journal.json");
        }

        private static void Write(string path, SourceSaveReceipt receipt)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            var bytes = new UTF8Encoding(false).GetBytes(JsonUtility.ToJson(receipt));
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
            if (File.Exists(path)) File.Replace(temporary, path, null);
            else File.Move(temporary, path);
        }

        private sealed class SourceOperation : IEditorJobOperation
        {
            private readonly SourceJobPayload _payload;
            private readonly SourceEditContext _context;
            private readonly EditorJobExecution _execution;
            private readonly ISourceEditBackend _backend;
            private readonly bool _recovering;
            private EditorJobPollResult _result;

            public SourceOperation(SourceJobPayload payload, SourceEditContext context, EditorJobExecution execution,
                ISourceEditBackend backend, bool recovering)
            {
                _payload = payload; _context = context; _execution = execution; _backend = backend; _recovering = recovering;
            }

            public EditorJobPollResult Poll(TimeSpan mainThreadBudget)
            {
                if (_result != null) return _result;
                try
                {
                    var receipt = _recovering ? Recover(_payload, _context, _execution, _backend) : Begin(_payload, _context, _execution, _backend);
                    var receiptPath = Path.Combine(_execution.Attempt.attemptRoot, "source-save-receipt.json");
                    Write(receiptPath, receipt);
                    _result = EditorJobPollResult.Completed(JsonUtility.ToJson(receipt), new[] {
                        new EditorJobArtifact { artifactId = _execution.Request.jobId + "-source-receipt",
                            kind = "source_save_receipt", mediaType = "application/json", path = receiptPath }
                    });
                    return _result;
                }
                finally
                {
                    var disposable = _backend as IDisposable;
                    if (disposable != null) disposable.Dispose();
                }
            }
        }
    }
}
#endif
