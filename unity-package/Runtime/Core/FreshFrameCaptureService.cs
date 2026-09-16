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
        private sealed class CaptureWriteState
        {
            private readonly object _sync = new object();
            private int _publicationRejected;
            private int _temporaryCreated;
            private int _published;
            private string _temporaryPath;
            private string _publishedPath;
            private string _publishedSha256;
            private long _publishedSize;

            public bool PublicationRejected
            {
                get { return Volatile.Read(ref _publicationRejected) != 0; }
            }

            public bool TemporaryCreated
            {
                get { return Volatile.Read(ref _temporaryCreated) != 0; }
            }

            public bool Published
            {
                get { return Volatile.Read(ref _published) != 0; }
            }

            public string TemporaryPath
            {
                get { lock (_sync) return _temporaryPath; }
            }

            public string PublishedPath
            {
                get { lock (_sync) return _publishedPath; }
            }

            public string PublishedSha256
            {
                get { lock (_sync) return _publishedSha256; }
            }

            public long PublishedSize
            {
                get { lock (_sync) return _publishedSize; }
            }

            public void RejectPublication()
            {
                Interlocked.Exchange(ref _publicationRejected, 1);
            }

            public void MarkTemporaryCreated(string path)
            {
                lock (_sync) _temporaryPath = path;
                Interlocked.Exchange(ref _temporaryCreated, 1);
            }

            public void MarkPublished(string path, string sha256, long size)
            {
                lock (_sync)
                {
                    _publishedPath = path;
                    _publishedSha256 = sha256;
                    _publishedSize = size;
                }

                Interlocked.Exchange(ref _published, 1);
            }
        }

        private sealed class EncodedFrame
        {
            public string TemporaryPath { get; private set; }
            public string Sha256 { get; private set; }
            public long Size { get; private set; }

            public EncodedFrame(string temporaryPath, string sha256, long size)
            {
                TemporaryPath = temporaryPath;
                Sha256 = sha256;
                Size = size;
            }
        }

        private sealed class ViewportRead
        {
            public long Value { get; private set; }
            public RelayLiveLoopError Error { get; private set; }

            public ViewportRead(long value, RelayLiveLoopError error)
            {
                Value = value;
                Error = error;
            }
        }

        private RuntimeSessionIdentity _identity;
        private IRelayLiveLoopMainThreadGuard _mainThread;
        private Func<long> _readViewportGeneration;
        private string _artifactRoot;
        private int _maximumPixels = 4 * 1024 * 1024;
        private int _busy;
        private bool _initialized;

#if RELAYLIVELOOP_SYNTHETIC
        internal static Func<int, int, Color32[], byte[]> SyntheticEncoderOverride;
#endif

        /// <summary>Legacy neutral overload retained for existing callers.</summary>
        public void InitializeOnMainThread(
            RuntimeSessionIdentity identity,
            IRelayLiveLoopMainThreadGuard mainThread,
            string artifactRoot,
            int maximumPixels = 4 * 1024 * 1024)
        {
            InitializeOnMainThread(identity, mainThread, artifactRoot, () => 1L, maximumPixels);
        }

        public void InitializeOnMainThread(
            RuntimeSessionIdentity identity,
            IRelayLiveLoopMainThreadGuard mainThread,
            string artifactRoot,
            Func<long> readViewportGeneration,
            int maximumPixels = 4 * 1024 * 1024)
        {
            if (identity == null) throw new ArgumentNullException(nameof(identity));
            if (mainThread == null) throw new ArgumentNullException(nameof(mainThread));
            mainThread.AssertMainThread();
            if (string.IsNullOrWhiteSpace(artifactRoot)) throw new ArgumentException("Artifact root is required.", nameof(artifactRoot));
            if (readViewportGeneration == null) throw new ArgumentNullException(nameof(readViewportGeneration));
            if (maximumPixels < 64 * 64) throw new ArgumentOutOfRangeException(nameof(maximumPixels));
            _identity = identity;
            _mainThread = mainThread;
            _readViewportGeneration = readViewportGeneration;
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
                return Task.FromResult(Failure(
                    RelayLiveLoopErrorCode.Timeout,
                    "capture_frame",
                    "Capture was cancelled before start.",
                    true));
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
            try
            {
                StartCoroutine(CaptureRoutine(request, cancellationToken, completion));
            }
            catch (Exception exception)
            {
                Interlocked.Exchange(ref _busy, 0);
                completion.TrySetResult(Failure(
                    RelayLiveLoopErrorCode.InternalError,
                    "capture_frame_start",
                    exception.GetType().Name + ": " + exception.Message,
                    true));
            }

            return completion.Task;
        }

        private IEnumerator CaptureRoutine(
            FreshFrameCaptureRequest request,
            CancellationToken cancellationToken,
            TaskCompletionSource<RelayLiveLoopResult<FreshFrameArtifact>> completion)
        {
            var startedAt = Time.realtimeSinceStartupAsDouble;
            var outputPath = Path.Combine(_artifactRoot, request.ArtifactId + ".png");
            CaptureWriteState writeState = null;
            Task<RelayLiveLoopResult<EncodedFrame>> writer = null;
            CancellationTokenSource writerCancellation = null;
            var cleanupAttached = false;
            try
            {
                while (Time.frameCount <= request.MinimumFrameExclusive)
                {
                    var boundary = CheckCaptureBoundary(
                        request,
                        request.ExpectedViewportGeneration,
                        "capture_frame_wait",
                        cancellationToken,
                        startedAt);
                    if (boundary != null)
                    {
                        completion.TrySetResult(Failure(boundary));
                        yield break;
                    }

                    yield return null;
                }

                var beforeEndOfFrame = CheckCaptureBoundary(
                    request,
                    request.ExpectedViewportGeneration,
                    "capture_frame_wait",
                    cancellationToken,
                    startedAt);
                if (beforeEndOfFrame != null)
                {
                    completion.TrySetResult(Failure(beforeEndOfFrame));
                    yield break;
                }

                yield return new WaitForEndOfFrame();

                var afterEndOfFrame = CheckCaptureBoundary(
                    request,
                    request.ExpectedViewportGeneration,
                    "capture_frame_pixels",
                    cancellationToken,
                    startedAt);
                if (afterEndOfFrame != null)
                {
                    completion.TrySetResult(Failure(afterEndOfFrame));
                    yield break;
                }

                var revision = _identity.RuntimeRevisions.CaptureIfCurrent(
                    request.ExpectedRuntimeRevision,
                    "capture_frame_pixels");
                if (!revision.Succeeded)
                {
                    completion.TrySetResult(RelayLiveLoopResult<FreshFrameArtifact>.Failure(revision.Error));
                    yield break;
                }

                var viewport = ReadViewportGeneration(request.ExpectedViewportGeneration, "capture_frame_viewport");
                if (viewport.Error != null)
                {
                    completion.TrySetResult(Failure(viewport.Error));
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
                catch (Exception exception)
                {
                    SafeDestroy(texture);
                    completion.TrySetResult(Failure(
                        exception is InvalidDataException ? RelayLiveLoopErrorCode.MessageTooLarge : RelayLiveLoopErrorCode.StateUnknown,
                        "capture_frame_pixels",
                        exception.GetType().Name + ": " + exception.Message,
                        true));
                    yield break;
                }

                SafeDestroy(texture);
                var beforeEncoding = CheckCaptureBoundary(
                    request,
                    viewport.Value,
                    "capture_frame_encode",
                    cancellationToken,
                    startedAt);
                if (beforeEncoding != null)
                {
                    completion.TrySetResult(Failure(beforeEncoding));
                    yield break;
                }

                writeState = new CaptureWriteState();
                writerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                try
                {
                    writer = Task.Run(() => EncodeAndWrite(
                        outputPath,
                        width,
                        height,
                        pixels,
                        writeState,
                        writerCancellation.Token));
                }
                catch (Exception exception)
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(Failure(
                        RelayLiveLoopErrorCode.InternalError,
                        "capture_frame_write",
                        exception.GetType().Name + ": " + exception.Message,
                        true));
                    yield break;
                }

                while (!writer.IsCompleted)
                {
                    var boundary = CheckCaptureBoundary(
                        request,
                        viewport.Value,
                        "capture_frame_encode",
                        cancellationToken,
                        startedAt);
                    if (boundary != null)
                    {
                        writeState.RejectPublication();
                        writerCancellation.Cancel();
                        AttachWriterCleanup(writer, writeState, writerCancellation);
                        cleanupAttached = true;
                        completion.TrySetResult(Failure(boundary));
                        yield break;
                    }

                    yield return null;
                }

                RelayLiveLoopResult<EncodedFrame> encoded;
                if (writer.IsCanceled)
                {
                    encoded = Failure<EncodedFrame>(
                        RelayLiveLoopErrorCode.Timeout,
                        "capture_frame_write",
                        "Capture was cancelled before encoding completed.",
                        true);
                }
                else if (writer.IsFaulted)
                {
                    encoded = Failure<EncodedFrame>(
                        RelayLiveLoopErrorCode.InternalError,
                        "capture_frame_write",
                        writer.Exception == null ? "Screenshot writer failed." : writer.Exception.GetBaseException().Message,
                        true);
                }
                else
                {
                    encoded = writer.Result;
                }

                if (encoded == null || !encoded.Succeeded || encoded.Value == null)
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(encoded == null
                        ? Failure(RelayLiveLoopErrorCode.ContractMismatch, "capture_frame_write", "Screenshot writer returned no result.", false)
                        : Failure(encoded.Error));
                    yield break;
                }

                var beforePublication = CheckCaptureBoundary(
                    request,
                    viewport.Value,
                    "capture_frame_publish",
                    cancellationToken,
                    startedAt);
                if (beforePublication != null)
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(Failure(beforePublication));
                    yield break;
                }

                if (File.Exists(outputPath))
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(Failure(
                        RelayLiveLoopErrorCode.InputChanged,
                        "capture_frame_publish",
                        "Artifact id was published concurrently.",
                        false));
                    yield break;
                }

                try
                {
                    File.Move(encoded.Value.TemporaryPath, outputPath);
                    writeState.MarkPublished(outputPath, encoded.Value.Sha256, encoded.Value.Size);
                }
                catch (Exception exception)
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(Failure(
                        RelayLiveLoopErrorCode.InternalError,
                        "capture_frame_publish",
                        exception.GetType().Name + ": " + exception.Message,
                        true));
                    yield break;
                }

                var afterPublication = CheckCaptureBoundary(
                    request,
                    viewport.Value,
                    "capture_frame_publish",
                    cancellationToken,
                    startedAt);
                if (afterPublication != null)
                {
                    writeState.RejectPublication();
                    completion.TrySetResult(Failure(afterPublication));
                    yield break;
                }

                completion.TrySetResult(RelayLiveLoopResult<FreshFrameArtifact>.Success(new FreshFrameArtifact
                {
                    ArtifactId = request.ArtifactId,
                    Kind = "screenshot",
                    MediaType = "image/png",
                    Path = outputPath,
                    Sha256 = encoded.Value.Sha256,
                    Size = encoded.Value.Size,
                    Frame = capturedFrame,
                    Width = width,
                    Height = height,
                    Fresh = capturedFrame > request.MinimumFrameExclusive,
                    RuntimeRevision = revision.Value,
                    ViewportGeneration = viewport.Value,
                    PublishedAtUnixMilliseconds = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                }));
            }
            finally
            {
                if (writeState != null && (!writeState.Published || writeState.PublicationRejected))
                {
                    writeState.RejectPublication();
                    if (writer != null && !writer.IsCompleted && !cleanupAttached)
                    {
                        if (writerCancellation != null) writerCancellation.Cancel();
                        AttachWriterCleanup(writer, writeState, writerCancellation);
                        cleanupAttached = true;
                    }
                    CleanupWriteState(writeState);
                }

                if (!completion.Task.IsCompleted)
                {
                    completion.TrySetResult(Failure(
                        RelayLiveLoopErrorCode.StateUnknown,
                        "capture_frame",
                        "Capture coroutine terminated without a terminal result.",
                        true));
                }

                Interlocked.Exchange(ref _busy, 0);
                if (writerCancellation != null && (writer == null || writer.IsCompleted)) writerCancellation.Dispose();
            }
        }

        private RelayLiveLoopError CheckCaptureBoundary(
            FreshFrameCaptureRequest request,
            long expectedViewportGeneration,
            string stage,
            CancellationToken cancellationToken,
            double startedAt)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return Error(RelayLiveLoopErrorCode.Timeout, stage, "Capture was cancelled before publication.", true);
            }

            if (Time.realtimeSinceStartupAsDouble - startedAt > request.Timeout.TotalSeconds)
            {
                return Error(RelayLiveLoopErrorCode.Timeout, stage, "Fresh-frame capture exceeded its bounded timeout.", true);
            }

            var revision = _identity.RuntimeRevisions.CaptureIfCurrent(request.ExpectedRuntimeRevision, stage);
            if (!revision.Succeeded) return revision.Error;
            if (expectedViewportGeneration <= 0) return null;
            return ReadViewportGeneration(expectedViewportGeneration, stage).Error;
        }

        private ViewportRead ReadViewportGeneration(long expected, string stage)
        {
            long actual;
            try
            {
                actual = _readViewportGeneration();
            }
            catch (Exception exception)
            {
                return new ViewportRead(0, Error(
                    RelayLiveLoopErrorCode.StateUnknown,
                    stage,
                    "Viewport generation reader failed: " + exception.GetType().Name + ": " + exception.Message,
                    true));
            }

            if (actual <= 0)
            {
                return new ViewportRead(0, Error(
                    RelayLiveLoopErrorCode.StateUnknown,
                    stage,
                    "Viewport generation reader returned an invalid generation.",
                    true));
            }

            if (expected > 0 && actual != expected)
            {
                return new ViewportRead(actual, Error(
                    RelayLiveLoopErrorCode.InputChanged,
                    stage,
                    "Viewport generation changed during capture.",
                    true));
            }

            return new ViewportRead(actual, null);
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
                request.Timeout > TimeSpan.FromMinutes(1) || request.ExpectedViewportGeneration <= 0)
            {
                return Error(RelayLiveLoopErrorCode.InvalidMessage, "capture_frame", "Capture bounds, timeout, or viewport generation are invalid.", false);
            }

            if (File.Exists(Path.Combine(_artifactRoot, request.ArtifactId + ".png")))
            {
                return Error(RelayLiveLoopErrorCode.InputChanged, "capture_frame", "Artifact id already exists and will not be overwritten.", false);
            }

            return null;
        }

        private static RelayLiveLoopResult<EncodedFrame> EncodeAndWrite(
            string outputPath,
            int width,
            int height,
            Color32[] pixels,
            CaptureWriteState state,
            CancellationToken cancellationToken)
        {
            if (cancellationToken.IsCancellationRequested || state.PublicationRejected)
            {
                return Failure<EncodedFrame>(RelayLiveLoopErrorCode.Timeout, "capture_frame_write", "Capture was cancelled before encoding.", true);
            }

            string temporary = null;
            try
            {
                byte[] png;
#if RELAYLIVELOOP_SYNTHETIC
                var overrideEncoder = SyntheticEncoderOverride;
                png = overrideEncoder == null
                    ? ManagedPngEncoder.EncodeRgba32(width, height, pixels, true)
                    : overrideEncoder(width, height, pixels);
#else
                png = ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
#endif
                temporary = outputPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    state.MarkTemporaryCreated(temporary);
                    stream.Write(png, 0, png.Length);
                    stream.Flush(true);
                }

                if (cancellationToken.IsCancellationRequested || state.PublicationRejected)
                {
                    TryDelete(temporary);
                    return Failure<EncodedFrame>(RelayLiveLoopErrorCode.Timeout, "capture_frame_write", "Capture was cancelled before publication.", true);
                }

                return RelayLiveLoopResult<EncodedFrame>.Success(new EncodedFrame(temporary, Sha256(png), png.LongLength));
            }
            catch (Exception exception)
            {
                if (state.TemporaryCreated) TryDelete(state.TemporaryPath);
                return Failure<EncodedFrame>(RelayLiveLoopErrorCode.InternalError, "capture_frame_write", exception.GetType().Name + ": " + exception.Message, true);
            }
        }

        private static void AttachWriterCleanup(
            Task<RelayLiveLoopResult<EncodedFrame>> writer,
            CaptureWriteState state,
            CancellationTokenSource cancellation)
        {
            if (writer == null) return;
            writer.ContinueWith(
                ignored =>
                {
                    state.RejectPublication();
                    CleanupWriteState(state);
                    if (cancellation != null) cancellation.Dispose();
                },
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }

        private static void CleanupWriteState(CaptureWriteState state)
        {
            if (state == null) return;
            if (state.TemporaryCreated && !string.IsNullOrWhiteSpace(state.TemporaryPath))
            {
                TryDelete(state.TemporaryPath);
            }

            if (state.Published && !string.IsNullOrWhiteSpace(state.PublishedPath) &&
                IsOwnedFile(state.PublishedPath, state.PublishedSha256, state.PublishedSize))
            {
                TryDelete(state.PublishedPath);
            }
        }

        private static bool IsOwnedFile(string path, string expectedSha256, long expectedSize)
        {
            try
            {
                return File.Exists(path) && new FileInfo(path).Length == expectedSize &&
                    string.Equals(Sha256File(path), expectedSha256, StringComparison.Ordinal);
            }
            catch
            {
                return false;
            }
        }

        private static string Sha256File(string path)
        {
            using (var sha = SHA256.Create())
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var hash = sha.ComputeHash(stream);
                var builder = new StringBuilder(hash.Length * 2);
                for (var index = 0; index < hash.Length; index++)
                {
                    builder.Append(hash[index].ToString("x2", CultureInfo.InvariantCulture));
                }

                return builder.ToString();
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path)) File.Delete(path);
            }
            catch
            {
                // Only operation-owned temporary/output paths are ever passed here.
            }
        }

        private static void SafeDestroy(Texture2D texture)
        {
            if (texture == null) return;
            try { Destroy(texture); }
            catch { }
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

        private static RelayLiveLoopResult<FreshFrameArtifact> Failure(RelayLiveLoopError error)
        {
            return RelayLiveLoopResult<FreshFrameArtifact>.Failure(error);
        }

        private static RelayLiveLoopResult<T> Failure<T>(
            RelayLiveLoopErrorCode code,
            string stage,
            string message,
            bool recoverable)
        {
            return RelayLiveLoopResult<T>.Failure(Error(code, stage, message, recoverable));
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
