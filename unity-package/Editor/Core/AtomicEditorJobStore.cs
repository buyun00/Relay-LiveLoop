#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class EditorJobClaim
    {
        internal EditorJobClaim(
            EditorJobRequest request,
            string requestDigest,
            string processingPath,
            EditorJobAttemptRecord attempt,
            bool recovering,
            string recoveryProblem)
        {
            Request = request;
            RequestDigest = requestDigest;
            ProcessingPath = processingPath;
            Attempt = attempt;
            IsRecovering = recovering;
            RecoveryProblem = recoveryProblem;
        }

        public EditorJobRequest Request { get; private set; }
        public string RequestDigest { get; private set; }
        public string ProcessingPath { get; private set; }
        public EditorJobAttemptRecord Attempt { get; internal set; }
        public bool IsRecovering { get; private set; }
        public string RecoveryProblem { get; private set; }
    }

    internal sealed class EditorArtifactSealResult
    {
        public List<EditorJobArtifact> Artifacts;
        public EditorJobError Error;
    }

    public sealed class AtomicEditorJobStore
    {
        private const int MaximumRequestBytes = 1024 * 1024;
        private readonly string _root;
        private readonly string _allowedArtifactRoot;
        private readonly string _incoming;
        private readonly string _processing;
        private readonly string _results;
        private readonly string _archive;
        private readonly string _invalid;
        private readonly string _attemptTombstones;

        public AtomicEditorJobStore(string root, string allowedArtifactRoot)
        {
            _root = NormalizeRoot(root, nameof(root));
            _allowedArtifactRoot = NormalizeRoot(allowedArtifactRoot, nameof(allowedArtifactRoot));
            _incoming = Path.Combine(_root, "incoming");
            _processing = Path.Combine(_root, "processing");
            _results = Path.Combine(_root, "results");
            _archive = Path.Combine(_root, "archive");
            _invalid = Path.Combine(_root, "invalid");
            _attemptTombstones = Path.Combine(_root, "attempt-tombstones");
            Directory.CreateDirectory(_incoming);
            Directory.CreateDirectory(_processing);
            Directory.CreateDirectory(_results);
            Directory.CreateDirectory(_archive);
            Directory.CreateDirectory(_invalid);
            Directory.CreateDirectory(_attemptTombstones);
            Directory.CreateDirectory(_allowedArtifactRoot);
        }

        public string IncomingDirectory { get { return _incoming; } }

        public EditorJobClaim TryGetRecovery(Func<EditorJobRequest, bool> canRun)
        {
            if (canRun == null) throw new ArgumentNullException(nameof(canRun));
            var files = Directory.GetFiles(_processing, "*.request.json");
            Array.Sort(files, StringComparer.Ordinal);
            for (var index = 0; index < files.Length; index++)
            {
                EditorJobRequest request;
                string digest;
                string validationError;
                if (!TryReadRequest(files[index], out request, out digest, out validationError))
                {
                    PreserveInvalid(files[index], "processing-request");
                    continue;
                }

                if (HasMatchingResult(request, digest))
                {
                    PreserveArchive(files[index], request.jobId + ".completed-processing-request.json");
                    var completedAttemptPath = GetAttemptPath(request.jobId);
                    if (File.Exists(completedAttemptPath))
                    {
                        PreserveArchive(completedAttemptPath, request.jobId + ".completed-processing-attempt.json");
                    }
                    continue;
                }

                if (!canRun(request)) continue;
                var attemptPath = GetAttemptPath(request.jobId);
                EditorJobAttemptRecord attempt = null;
                string recoveryProblem = null;
                if (File.Exists(attemptPath))
                {
                    try
                    {
                        attempt = JsonUtility.FromJson<EditorJobAttemptRecord>(File.ReadAllText(attemptPath, Encoding.UTF8));
                    }
                    catch (Exception ex)
                    {
                        recoveryProblem = "Attempt record cannot be read: " + ex.GetType().Name;
                    }

                    if (attempt == null ||
                        !string.Equals(attempt.jobId, request.jobId, StringComparison.Ordinal) ||
                        !string.Equals(attempt.requestDigest, digest, StringComparison.OrdinalIgnoreCase) ||
                        !string.Equals(attempt.inputSnapshot, request.inputSnapshot, StringComparison.Ordinal) ||
                        !string.Equals(attempt.providerId, request.providerId, StringComparison.Ordinal))
                    {
                        recoveryProblem = "Attempt record does not match the processing request.";
                        attempt = null;
                    }
                }

                var tombstonePath = GetAttemptTombstonePath(request.jobId);
                if (File.Exists(tombstonePath))
                {
                    string tombstoneProblem;
                    var tombstone = ReadAttemptTombstone(request.jobId, out tombstoneProblem);
                    if (!string.IsNullOrEmpty(tombstoneProblem) ||
                        !AttemptMatchesRequest(tombstone, request, digest))
                    {
                        if (File.Exists(attemptPath)) PreserveInvalid(attemptPath, "attempt-tombstone-mismatch");
                        PreserveInvalid(files[index], "attempt-tombstone-mismatch");
                        continue;
                    }

                    if (attempt == null)
                    {
                        attempt = tombstone;
                        recoveryProblem = "A durable prior-attempt tombstone exists without a live processing attempt; fresh Begin is forbidden.";
                    }
                    else if (!SameAttempt(attempt, tombstone))
                    {
                        recoveryProblem = "The active attempt differs from its durable prior-attempt tombstone.";
                    }
                }

                // No attempt record means the atomic claim completed but provider execution never
                // became eligible only when no durable attempt tombstone exists.
                return new EditorJobClaim(request, digest, files[index], attempt, attempt != null, recoveryProblem);
            }

            return null;
        }

        public EditorJobClaim TryClaimNext(Func<EditorJobRequest, bool> canRun)
        {
            if (canRun == null) throw new ArgumentNullException(nameof(canRun));
            var files = Directory.GetFiles(_incoming, "*.request.json");
            Array.Sort(files, StringComparer.Ordinal);
            for (var index = 0; index < files.Length; index++)
            {
                EditorJobRequest request;
                string digest;
                string validationError;
                if (!TryReadRequest(files[index], out request, out digest, out validationError))
                {
                    PreserveInvalid(files[index], "incoming-request");
                    continue;
                }

                if (!canRun(request)) continue;
                if (HasMatchingResult(request, digest))
                {
                    PreserveArchive(files[index], request.jobId + ".duplicate-request.json");
                    continue;
                }

                var tombstonePath = GetAttemptTombstonePath(request.jobId);
                if (File.Exists(tombstonePath))
                {
                    string tombstoneProblem;
                    var tombstone = ReadAttemptTombstone(request.jobId, out tombstoneProblem);
                    if (!string.IsNullOrEmpty(tombstoneProblem) ||
                        !AttemptMatchesRequest(tombstone, request, digest))
                    {
                        PreserveInvalid(files[index], "attempt-tombstone-mismatch");
                        continue;
                    }

                    var duplicateProcessingPath = Path.Combine(_processing, request.jobId + ".request.json");
                    try
                    {
                        File.Move(files[index], duplicateProcessingPath);
                    }
                    catch (IOException)
                    {
                        continue;
                    }

                    return new EditorJobClaim(
                        request,
                        digest,
                        duplicateProcessingPath,
                        tombstone,
                        true,
                        "A durable prior-attempt tombstone exists; fresh Begin is forbidden.");
                }

                var processingPath = Path.Combine(_processing, request.jobId + ".request.json");
                try
                {
                    File.Move(files[index], processingPath);
                }
                catch (IOException)
                {
                    continue;
                }

                return new EditorJobClaim(request, digest, processingPath, null, false, null);
            }

            return null;
        }

        public EditorJobAttemptRecord BeginAttempt(EditorJobClaim claim)
        {
            if (claim == null) throw new ArgumentNullException(nameof(claim));
            if (claim.Attempt != null)
            {
                if (string.IsNullOrEmpty(claim.RecoveryProblem)) PersistAttemptTombstone(claim.Attempt);
                return claim.Attempt;
            }
            var tombstonePath = GetAttemptTombstonePath(claim.Request.jobId);
            if (File.Exists(tombstonePath))
            {
                string tombstoneProblem;
                var prior = ReadAttemptTombstone(claim.Request.jobId, out tombstoneProblem);
                if (!string.IsNullOrEmpty(tombstoneProblem) || !AttemptMatchesRequest(prior, claim.Request, claim.RequestDigest))
                {
                    throw new InvalidDataException("A prior-attempt tombstone prevents a fresh attempt for this job id.");
                }

                claim.Attempt = prior;
                return prior;
            }
            var attemptId = Guid.NewGuid().ToString("N");
            var inputSegment = claim.RequestDigest.Substring(0, 16);
            var artifactRoot = Path.GetFullPath(claim.Request.artifactRoot);
            var attemptRoot = Path.GetFullPath(Path.Combine(
                artifactRoot,
                claim.Request.jobId,
                inputSegment,
                attemptId));
            EnsureContained(_allowedArtifactRoot, attemptRoot, "Attempt root is outside the configured artifact root.");
            Directory.CreateDirectory(attemptRoot);
            var attempt = new EditorJobAttemptRecord
            {
                jobId = claim.Request.jobId,
                requestDigest = claim.RequestDigest,
                inputSnapshot = claim.Request.inputSnapshot,
                providerId = claim.Request.providerId,
                attemptId = attemptId,
                attemptRoot = attemptRoot,
                startedAtUtc = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                state = "started"
            };
            AtomicWriteJson(GetAttemptPath(claim.Request.jobId), attempt);
            PersistAttemptTombstone(attempt);
            claim.Attempt = attempt;
            return attempt;
        }

        internal Task<EditorArtifactSealResult> SealArtifactsAsync(
            EditorJobClaim claim,
            IReadOnlyList<EditorJobArtifact> artifacts)
        {
            if (claim == null) throw new ArgumentNullException(nameof(claim));
            if (claim.Attempt == null) throw new InvalidOperationException("Attempt must be recorded before sealing artifacts.");
            var copy = artifacts == null
                ? new List<EditorJobArtifact>()
                : new List<EditorJobArtifact>(artifacts);
            var attemptRoot = claim.Attempt.attemptRoot;
            return Task.Run(() => SealArtifacts(attemptRoot, copy));
        }

        public void PublishSuccess(
            EditorJobClaim claim,
            string resultJson,
            IReadOnlyList<EditorJobArtifact> sealedArtifacts)
        {
            if (claim == null) throw new ArgumentNullException(nameof(claim));
            if (claim.Attempt == null) throw new InvalidOperationException("Attempt is missing.");
            var result = BuildResult(claim, "completed", resultJson, null, sealedArtifacts);
            Publish(claim, result);
        }

        public void PublishFailure(EditorJobClaim claim, EditorJobError error)
        {
            if (claim == null) throw new ArgumentNullException(nameof(claim));
            if (claim.Attempt == null) BeginAttempt(claim);
            if (error == null) throw new ArgumentNullException(nameof(error));
            var status = string.Equals(error.code, "STATE_UNKNOWN", StringComparison.Ordinal)
                ? "state_unknown"
                : "failed";
            Publish(claim, BuildResult(claim, status, null, error, null));
        }

        public bool HasMatchingResult(EditorJobRequest request, string requestDigest)
        {
            var path = Path.Combine(_results, request.jobId + ".result.json");
            if (!File.Exists(path)) return false;
            try
            {
                var result = JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(path, Encoding.UTF8));
                return result != null &&
                    string.Equals(result.jobId, request.jobId, StringComparison.Ordinal) &&
                    string.Equals(result.requestDigest, requestDigest, StringComparison.OrdinalIgnoreCase) &&
                    string.Equals(result.inputSnapshot, request.inputSnapshot, StringComparison.Ordinal) &&
                    string.Equals(result.providerId, request.providerId, StringComparison.Ordinal);
            }
            catch
            {
                return false;
            }
        }

        private void Publish(EditorJobClaim claim, EditorJobResult result)
        {
            var resultPath = Path.Combine(_results, claim.Request.jobId + ".result.json");
            if (File.Exists(resultPath) && !HasMatchingResult(claim.Request, claim.RequestDigest))
            {
                PreserveInvalid(resultPath, "stale-result");
            }

            AtomicWriteJson(resultPath, result);
            var archiveName = claim.Request.jobId + "." + claim.Attempt.attemptId + ".request.json";
            PreserveArchive(claim.ProcessingPath, archiveName);
            var attemptPath = GetAttemptPath(claim.Request.jobId);
            if (File.Exists(attemptPath))
            {
                PreserveArchive(attemptPath, claim.Request.jobId + "." + claim.Attempt.attemptId + ".attempt.json");
            }
        }

        private EditorJobResult BuildResult(
            EditorJobClaim claim,
            string status,
            string resultJson,
            EditorJobError error,
            IReadOnlyList<EditorJobArtifact> artifacts)
        {
            return new EditorJobResult
            {
                jobId = claim.Request.jobId,
                requestDigest = claim.RequestDigest,
                inputSnapshot = claim.Request.inputSnapshot,
                providerId = claim.Request.providerId,
                attemptId = claim.Attempt.attemptId,
                status = status,
                completedAtUtc = DateTimeOffset.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                resultJson = resultJson,
                error = error,
                artifacts = artifacts == null
                    ? new List<EditorJobArtifact>()
                    : new List<EditorJobArtifact>(artifacts)
            };
        }

        private bool TryReadRequest(
            string path,
            out EditorJobRequest request,
            out string digest,
            out string validationError)
        {
            request = null;
            digest = null;
            validationError = null;
            try
            {
                var info = new FileInfo(path);
                if (info.Length <= 0 || info.Length > MaximumRequestBytes)
                {
                    validationError = "Request file size is outside the allowed bounds.";
                    return false;
                }

                var bytes = File.ReadAllBytes(path);
                digest = ComputeSha256(bytes);
                request = JsonUtility.FromJson<EditorJobRequest>(Encoding.UTF8.GetString(bytes));
                validationError = ValidateRequest(request);
                return validationError == null;
            }
            catch (Exception ex)
            {
                validationError = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }

        private string ValidateRequest(EditorJobRequest request)
        {
            if (request == null) return "Request JSON is empty.";
            if (!IsSafeIdentifier(request.jobId)) return "Job id is invalid.";
            if (!IsSafeIdentifier(request.kind)) return "Job kind is invalid.";
            if (!IsSafeIdentifier(request.providerId)) return "Provider id is invalid.";
            if (string.IsNullOrWhiteSpace(request.inputSnapshot) || request.inputSnapshot.Length > 256)
            {
                return "Input snapshot is invalid.";
            }

            DateTimeOffset expires;
            if (!DateTimeOffset.TryParse(
                request.expiresAtUtc,
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out expires))
            {
                return "Expiry is invalid.";
            }

            if (expires <= DateTimeOffset.UtcNow) return "Request expired.";
            string artifactRoot;
            try
            {
                artifactRoot = Path.GetFullPath(request.artifactRoot ?? string.Empty);
                EnsureContained(_allowedArtifactRoot, artifactRoot, "Artifact root is outside the configured root.");
            }
            catch (Exception ex)
            {
                return ex.Message;
            }

            return null;
        }

        private static EditorArtifactSealResult SealArtifacts(
            string attemptRoot,
            IReadOnlyList<EditorJobArtifact> artifacts)
        {
            var sealedArtifacts = new List<EditorJobArtifact>();
            var ids = new HashSet<string>(StringComparer.Ordinal);
            try
            {
                for (var index = 0; index < artifacts.Count; index++)
                {
                    var artifact = artifacts[index];
                    if (artifact == null || !IsSafeIdentifier(artifact.artifactId) ||
                        string.IsNullOrWhiteSpace(artifact.kind) || string.IsNullOrWhiteSpace(artifact.path))
                    {
                        throw new InvalidDataException("Artifact manifest is incomplete.");
                    }

                    if (!ids.Add(artifact.artifactId)) throw new InvalidDataException("Artifact id is duplicated.");
                    var path = Path.GetFullPath(artifact.path);
                    EnsureContained(attemptRoot, path, "Artifact is outside this attempt's immutable root.");
                    if (!File.Exists(path)) throw new FileNotFoundException("Artifact file is missing.", path);
                    string sha;
                    long size;
                    using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                    {
                        size = stream.Length;
                        using (var hash = SHA256.Create()) sha = ToHex(hash.ComputeHash(stream));
                    }

                    if (!string.IsNullOrEmpty(artifact.sha256) &&
                        !string.Equals(artifact.sha256, sha, StringComparison.OrdinalIgnoreCase))
                    {
                        throw new InvalidDataException("Artifact hash does not match provider declaration.");
                    }

                    sealedArtifacts.Add(new EditorJobArtifact
                    {
                        artifactId = artifact.artifactId,
                        kind = artifact.kind,
                        path = path,
                        sha256 = sha,
                        mediaType = artifact.mediaType,
                        size = size
                    });
                }

                return new EditorArtifactSealResult { Artifacts = sealedArtifacts };
            }
            catch (Exception ex)
            {
                return new EditorArtifactSealResult
                {
                    Error = Error("CONTRACT_MISMATCH", "seal_artifacts", ex.Message, false)
                };
            }
        }

        internal static EditorJobError Error(string code, string stage, string message, bool recoverable)
        {
            return new EditorJobError
            {
                code = code,
                stage = stage,
                message = message,
                recoverable = recoverable,
                runtimeChangedKnown = false,
                runtimeChanged = false
            };
        }

        private static string NormalizeRoot(string root, string parameterName)
        {
            if (string.IsNullOrWhiteSpace(root)) throw new ArgumentException("Root is required.", parameterName);
            return Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }

        private static void EnsureContained(string root, string candidate, string message)
        {
            var normalizedRoot = NormalizeRoot(root, nameof(root));
            var normalizedCandidate = Path.GetFullPath(candidate);
            if (string.Equals(normalizedRoot, normalizedCandidate, StringComparison.OrdinalIgnoreCase)) return;
            var prefix = normalizedRoot + Path.DirectorySeparatorChar;
            if (!normalizedCandidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException(message);
            }
        }

        private static bool IsSafeIdentifier(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value.Length > 128) return false;
            for (var index = 0; index < value.Length; index++)
            {
                var c = value[index];
                if (!(char.IsLetterOrDigit(c) || c == '-' || c == '_' || c == '.')) return false;
            }

            return value != "." && value != "..";
        }

        private static string ComputeSha256(byte[] bytes)
        {
            using (var hash = SHA256.Create()) return ToHex(hash.ComputeHash(bytes));
        }

        private static string ToHex(byte[] bytes)
        {
            var builder = new StringBuilder(bytes.Length * 2);
            for (var index = 0; index < bytes.Length; index++)
            {
                builder.Append(bytes[index].ToString("x2", CultureInfo.InvariantCulture));
            }

            return builder.ToString();
        }

        private static void AtomicWriteJson<T>(string destination, T value)
        {
            var directory = Path.GetDirectoryName(destination);
            Directory.CreateDirectory(directory);
            var temporary = destination + "." + Guid.NewGuid().ToString("N") + ".tmp";
            var bytes = new UTF8Encoding(false).GetBytes(SerializeJson(value));
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }

            if (File.Exists(destination))
            {
                var backup = destination + ".bak";
                File.Replace(temporary, destination, backup, true);
                if (File.Exists(backup)) File.Delete(backup);
            }
            else
            {
                File.Move(temporary, destination);
            }
        }

        private static string SerializeJson<T>(T value)
        {
            var json = JsonUtility.ToJson(value, true);
            var result = value is EditorJobResult ? (EditorJobResult)(object)value : null;
            if (result == null || string.Equals(result.status, "completed", StringComparison.Ordinal) || result.resultJson != null)
            {
                return json;
            }

            // Unity JsonUtility emits a null string field as an empty string. The
            // Editor result contract requires literal JSON null for failed results;
            // completed results intentionally remain unchanged and are validated as
            // requiring non-empty JSON by the Host transport.
            var normalized = json.Replace("\"resultJson\": \"\"", "\"resultJson\": null");
            if (normalized == json)
            {
                normalized = json.Replace("\"resultJson\":\"\"", "\"resultJson\":null");
            }

            if (normalized == json)
            {
                throw new InvalidDataException("Failed Editor result did not serialize a resultJson string field.");
            }

            return normalized;
        }

        private string GetAttemptPath(string jobId)
        {
            return Path.Combine(_processing, jobId + ".attempt.json");
        }

        private string GetAttemptTombstonePath(string jobId)
        {
            return Path.Combine(_attemptTombstones, jobId + ".attempt.json");
        }

        private EditorJobAttemptRecord ReadAttemptTombstone(string jobId, out string problem)
        {
            problem = null;
            var path = GetAttemptTombstonePath(jobId);
            try
            {
                var value = JsonUtility.FromJson<EditorJobAttemptRecord>(File.ReadAllText(path, Encoding.UTF8));
                if (value == null ||
                    !string.Equals(value.jobId, jobId, StringComparison.Ordinal) ||
                    !IsSha256(value.requestDigest) ||
                    string.IsNullOrWhiteSpace(value.inputSnapshot) ||
                    !IsSafeIdentifier(value.providerId) ||
                    !IsSafeIdentifier(value.attemptId) ||
                    string.IsNullOrWhiteSpace(value.attemptRoot) ||
                    string.IsNullOrWhiteSpace(value.startedAtUtc) ||
                    string.IsNullOrWhiteSpace(value.state))
                {
                    problem = "Attempt tombstone fields are invalid.";
                    return null;
                }

                EnsureContained(_allowedArtifactRoot, value.attemptRoot, "Attempt tombstone root escaped the configured artifact root.");
                return value;
            }
            catch (Exception ex)
            {
                problem = "Attempt tombstone cannot be read: " + ex.GetType().Name;
                return null;
            }
        }

        private void PersistAttemptTombstone(EditorJobAttemptRecord attempt)
        {
            var path = GetAttemptTombstonePath(attempt.jobId);
            if (File.Exists(path))
            {
                string problem;
                var existing = ReadAttemptTombstone(attempt.jobId, out problem);
                if (!string.IsNullOrEmpty(problem) || !SameAttempt(existing, attempt))
                {
                    throw new InvalidDataException("Durable attempt tombstone differs from the active attempt.");
                }
                return;
            }

            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            var bytes = new UTF8Encoding(false).GetBytes(SerializeJson(attempt));
            try
            {
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true);
                }

                try
                {
                    File.Move(temporary, path);
                }
                catch (IOException)
                {
                    if (!File.Exists(path)) throw;
                    string problem;
                    var existing = ReadAttemptTombstone(attempt.jobId, out problem);
                    if (!string.IsNullOrEmpty(problem) || !SameAttempt(existing, attempt)) throw;
                }
            }
            finally
            {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
        }

        private static bool AttemptMatchesRequest(EditorJobAttemptRecord attempt, EditorJobRequest request, string digest)
        {
            return attempt != null && request != null &&
                string.Equals(attempt.jobId, request.jobId, StringComparison.Ordinal) &&
                string.Equals(attempt.requestDigest, digest, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(attempt.inputSnapshot, request.inputSnapshot, StringComparison.Ordinal) &&
                string.Equals(attempt.providerId, request.providerId, StringComparison.Ordinal);
        }

        private static bool SameAttempt(EditorJobAttemptRecord left, EditorJobAttemptRecord right)
        {
            return left != null && right != null &&
                string.Equals(left.jobId, right.jobId, StringComparison.Ordinal) &&
                string.Equals(left.requestDigest, right.requestDigest, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(left.inputSnapshot, right.inputSnapshot, StringComparison.Ordinal) &&
                string.Equals(left.providerId, right.providerId, StringComparison.Ordinal) &&
                string.Equals(left.attemptId, right.attemptId, StringComparison.Ordinal) &&
                string.Equals(left.attemptRoot, right.attemptRoot, StringComparison.OrdinalIgnoreCase) &&
                string.Equals(left.startedAtUtc, right.startedAtUtc, StringComparison.Ordinal) &&
                string.Equals(left.state, right.state, StringComparison.Ordinal);
        }

        private static bool IsSha256(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length != 64) return false;
            for (var index = 0; index < value.Length; index++)
            {
                var c = value[index];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
            }
            return true;
        }

        private void PreserveInvalid(string source, string label)
        {
            if (!File.Exists(source)) return;
            var name = Path.GetFileNameWithoutExtension(source) + "." + label + "." +
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture) + ".json";
            File.Move(source, UniquePath(_invalid, name));
        }

        private void PreserveArchive(string source, string name)
        {
            if (!File.Exists(source)) return;
            File.Move(source, UniquePath(_archive, name));
        }

        private static string UniquePath(string directory, string name)
        {
            var path = Path.Combine(directory, name);
            if (!File.Exists(path)) return path;
            return Path.Combine(directory, Guid.NewGuid().ToString("N") + "." + name);
        }
    }
}
#endif
