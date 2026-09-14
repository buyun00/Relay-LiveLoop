#if UNITY_EDITOR || DEVELOPMENT_BUILD
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RelayLiveLoop
{
    public sealed class FreshFrameCaptureService : MonoBehaviour, IFreshFrameCaptureProvider
    {
        private RuntimeSessionIdentity _identity;
        private IRelayLiveLoopMainThreadGuard _mainThread;
        private string _artifactRoot;
        private int _maximumPixels = 4 * 1024 * 1024;
        private int _busy;
        private bool _initialized;

        public void InitializeOnMainThread(
            RuntimeSessionIdentity identity,
            IRelayLiveLoopMainThreadGuard mainThread,
            string artifactRoot,
            int maximumPixels = 4 * 1024 * 1024)
        {
            if (identity == null) throw new ArgumentNullException(nameof(identity));
            if (mainThread == null) throw new ArgumentNullException(nameof(mainThread));
            mainThread.AssertMainThread();
            if (string.IsNullOrWhiteSpace(artifactRoot)) throw new ArgumentException("Artifact root is required.", nameof(artifactRoot));
            if (maximumPixels < 64 * 64) throw new ArgumentOutOfRangeException(nameof(maximumPixels));
            _identity = identity;
            _mainThread = mainThread;
            _artifactRoot = Path.GetFullPath(artifactRoot).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            Directory.CreateDirectory(_artifactRoot);
            _maximumPixels = maximumPixels;
            _initialized = true;
        }

        public Task<RelayLiveLoopResult<FreshFrameArtifact>> Capture(
            FreshFrameCaptureRequest request,
            CancellationToken cancellationToken)
        {
            if (!_initialized) throw new InvalidOperationException("Frame capture service is not initialized.");
            _mainThread.AssertMainThread();
            var validation = Validate(request);
            if (validation != null) return Task.FromResult(RelayLiveLoopResult<FreshFrameArtifact>.Failure(validation));
            if (cancellationToken.IsCancellationRequested)
            {
                return Task.FromCanceled<RelayLiveLoopResult<FreshFrameArtifact>>(cancellationToken);
            }

            if (Interlocked.CompareExchange(ref _busy, 1, 0) != 0)
            {
                return Task.FromResult(Failure(
                    RelayLiveLoopErrorCode.Busy,
                    "capture_frame",
                    "A frame capture is already in progress.",
                    true));
            }

            var completion = new TaskCompletionSource<RelayLiveLoopResult<FreshFrameArtifact>>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            StartCoroutine(CaptureRoutine(request, cancellationToken, completion));
            return completion.Task;
        }

        private IEnumerator CaptureRoutine(
            FreshFrameCaptureRequest request,
            CancellationToken cancellationToken,
            TaskCompletionSource<RelayLiveLoopResult<FreshFrameArtifact>> completion)
        {
            var startedAt = Time.realtimeSinceStartupAsDouble;
            while (Time.frameCount <= request.MinimumFrameExclusive)
            {
                if (cancellationToken.IsCancellationRequested)
                {
                    Interlocked.Exchange(ref _busy, 0);
                    completion.TrySetCanceled();
                    yield break;
                }

                if (Time.realtimeSinceStartupAsDouble - startedAt > request.Timeout.TotalSeconds)
                {
                    Interlocked.Exchange(ref _busy, 0);
                    completion.TrySetResult(Failure(
                        RelayLiveLoopErrorCode.Timeout,
                        "capture_frame_wait",
                        "No fresh rendered frame arrived before the timeout.",
                        true));
                    yield break;
                }

                yield return null;
            }

            yield return new WaitForEndOfFrame();
            if (cancellationToken.IsCancellationRequested)
            {
                Interlocked.Exchange(ref _busy, 0);
                completion.TrySetCanceled();
                yield break;
            }

            var revision = _identity.RuntimeRevisions.CaptureIfCurrent(
                request.ExpectedRuntimeRevision,
                "capture_frame_pixels");
            if (!revision.Succeeded)
            {
                Interlocked.Exchange(ref _busy, 0);
                completion.TrySetResult(RelayLiveLoopResult<FreshFrameArtifact>.Failure(revision.Error));
                yield break;
            }

            Texture2D texture = null;
            Color32[] pixels;
            int width;
            int height;
            long capturedFrame;
            try
            {
                texture = ScreenCapture.CaptureScreenshotAsTexture();
                if (texture == null) throw new InvalidOperationException("Unity did not return a screenshot texture.");
                width = texture.width;
                height = texture.height;
                capturedFrame = Time.frameCount;
                if (capturedFrame <= request.MinimumFrameExclusive)
                {
                    throw new InvalidOperationException("Captured frame is not newer than the requested baseline.");
                }

                if (width <= 0 || height <= 0 || width > request.MaximumWidth || height > request.MaximumHeight ||
                    checked(width * height) > _maximumPixels)
                {
                    throw new InvalidDataException("Captured frame exceeds the configured pixel bounds.");
                }

                pixels = texture.GetPixels32();
                if (pixels == null || pixels.Length != checked(width * height))
                {
                    throw new InvalidDataException("Screenshot pixel buffer is incomplete.");
                }
            }
            catch (Exception ex)
            {
                if (texture != null) Destroy(texture);
                Interlocked.Exchange(ref _busy, 0);
                completion.TrySetResult(Failure(
                    ex is InvalidDataException ? RelayLiveLoopErrorCode.MessageTooLarge : RelayLiveLoopErrorCode.StateUnknown,
                    "capture_frame_pixels",
                    ex.Message,
                    true));
                yield break;
            }

            Destroy(texture);
            var artifactId = request.ArtifactId;
            var outputPath = Path.Combine(_artifactRoot, artifactId + ".png");
            Task<RelayLiveLoopResult<FreshFrameArtifact>> writer = Task.Run(() =>
                EncodeAndWrite(
                    artifactId,
                    outputPath,
                    width,
                    height,
                    capturedFrame,
                    request.MinimumFrameExclusive,
                    revision.Value,
                    pixels,
                    cancellationToken));
            while (!writer.IsCompleted) yield return null;
            Interlocked.Exchange(ref _busy, 0);
            if (writer.IsCanceled)
            {
                completion.TrySetCanceled();
            }
            else if (writer.IsFaulted)
            {
                completion.TrySetResult(Failure(
                    RelayLiveLoopErrorCode.InternalError,
                    "capture_frame_write",
                    writer.Exception == null ? "Screenshot writer failed." : writer.Exception.GetBaseException().Message,
                    true));
            }
            else
            {
                completion.TrySetResult(writer.Result);
            }
        }

        private RelayLiveLoopError Validate(FreshFrameCaptureRequest request)
        {
            if (request == null || !IsSafeArtifactId(request.ArtifactId))
            {
                return Error(RelayLiveLoopErrorCode.InvalidMessage, "capture_frame", "Artifact id is invalid.", false);
            }

            if (!string.Equals(request.ExpectedSessionId, _identity.SessionId, StringComparison.Ordinal))
            {
                return Error(RelayLiveLoopErrorCode.WrongSession, "capture_frame", "Capture targets another Player session.", false);
            }

            var revision = _identity.RuntimeRevisions.CaptureIfCurrent(
                request.ExpectedRuntimeRevision,
                "capture_frame");
            if (!revision.Succeeded) return revision.Error;

            if (request.MinimumFrameExclusive < 0 || request.MaximumWidth <= 0 || request.MaximumHeight <= 0 ||
                request.MaximumWidth > 8192 || request.MaximumHeight > 8192 || request.Timeout <= TimeSpan.Zero ||
                request.Timeout > TimeSpan.FromMinutes(1))
            {
                return Error(RelayLiveLoopErrorCode.InvalidMessage, "capture_frame", "Capture bounds or timeout are invalid.", false);
            }

            if (File.Exists(Path.Combine(_artifactRoot, request.ArtifactId + ".png")))
            {
                return Error(RelayLiveLoopErrorCode.InputChanged, "capture_frame", "Artifact id already exists and will not be overwritten.", false);
            }

            return null;
        }

        private static RelayLiveLoopResult<FreshFrameArtifact> EncodeAndWrite(
            string artifactId,
            string outputPath,
            int width,
            int height,
            long capturedFrame,
            long minimumFrameExclusive,
            string runtimeRevision,
            Color32[] pixels,
            CancellationToken cancellationToken)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return Failure(RelayLiveLoopErrorCode.Timeout, "capture_frame_write", "Capture was cancelled before encoding.", true);
            }

            try
            {
                var png = ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
                if (cancellationToken.IsCancellationRequested)
                {
                    return Failure(RelayLiveLoopErrorCode.Timeout, "capture_frame_write", "Capture was cancelled before publication.", true);
                }

                var temporary = outputPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(png, 0, png.Length);
                    stream.Flush(true);
                }

                if (File.Exists(outputPath))
                {
                    File.Delete(temporary);
                    return Failure(RelayLiveLoopErrorCode.InputChanged, "capture_frame_write", "Artifact id was published concurrently.", false);
                }

                File.Move(temporary, outputPath);
                return RelayLiveLoopResult<FreshFrameArtifact>.Success(new FreshFrameArtifact
                {
                    ArtifactId = artifactId,
                    Kind = "screenshot",
                    MediaType = "image/png",
                    Path = outputPath,
                    Sha256 = Sha256(png),
                    Size = png.LongLength,
                    Frame = capturedFrame,
                    Width = width,
                    Height = height,
                    Fresh = capturedFrame > minimumFrameExclusive,
                    RuntimeRevision = runtimeRevision
                });
            }
            catch (Exception ex)
            {
                return Failure(RelayLiveLoopErrorCode.InternalError, "capture_frame_write", ex.GetType().Name + ": " + ex.Message, true);
            }
        }

        private static string Sha256(byte[] bytes)
        {
            using (var sha = SHA256.Create())
            {
                var hash = sha.ComputeHash(bytes);
                var builder = new StringBuilder(hash.Length * 2);
                for (var index = 0; index < hash.Length; index++)
                {
                    builder.Append(hash[index].ToString("x2", CultureInfo.InvariantCulture));
                }

                return builder.ToString();
            }
        }

        private static bool IsSafeArtifactId(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value.Length > 128) return false;
            for (var index = 0; index < value.Length; index++)
            {
                var c = value[index];
                if (!(char.IsLetterOrDigit(c) || c == '-' || c == '_')) return false;
            }

            return true;
        }

        private static RelayLiveLoopResult<FreshFrameArtifact> Failure(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable)
        {
            return RelayLiveLoopResult<FreshFrameArtifact>.Failure(Error(code, stage, message, recoverable));
        }

        private static RelayLiveLoopError Error(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable)
        {
            return new RelayLiveLoopError(code, stage, message, false, recoverable, Array.Empty<string>());
        }
    }
}
#endif
