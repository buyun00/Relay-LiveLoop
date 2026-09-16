#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using RelayLiveLoop;
using UnityEngine;

internal static class FreshFrameLifecycleSyntheticTests
{
    private static int _passed;

    public static int Main()
    {
        try
        {
            TestReaderFailureReleasesBusyAndAllowsRetry();
            TestEncoderFailureCleansTemporaryFile();
            TestGenerationChangeBeforePublicationCleansTemporaryFile();
            TestGenerationChangeAfterPublicationRemovesOwnedArtifact();
            TestCancellationAndTimeoutDoNotOccupyArtifactId();
            TestConcurrentCaptureReturnsBusyWithoutLeaking();
            Console.WriteLine("FRESH FRAME LIFECYCLE PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("FRESH FRAME LIFECYCLE FAIL: " + exception);
            return 1;
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
        }
    }

    private static void TestReaderFailureReleasesBusyAndAllowsRetry()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-reader", "launch-reader", "revision-reader", 1);
            var generation = 1L;
            var readerThrows = true;
            var capture = CreateCapture(identity, root, () =>
            {
                if (readerThrows) throw new InvalidOperationException("synthetic viewport read failure");
                return generation;
            });

            var failed = capture.Capture(Request(identity, "reader-retry"), CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.StateUnknown, failed.Error.Code, "viewport reader failure is state unknown");
            NoArtifacts(root, "reader failure has no artifact");

            readerThrows = false;
            var retried = capture.Capture(Request(identity, "reader-retry"), CancellationToken.None).GetAwaiter().GetResult();
            True(retried.Succeeded, "reader failure releases busy for retry");
            DeleteRoot(root);
        }
        finally
        {
            DeleteRoot(root);
        }
    }

    private static void TestEncoderFailureCleansTemporaryFile()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-encoder", "launch-encoder", "revision-encoder", 1);
            var capture = CreateCapture(identity, root, () => 1L);
            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) => throw new InvalidDataException("synthetic encoder failure");

            var failed = capture.Capture(Request(identity, "encoder-retry"), CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.InternalError, failed.Error.Code, "encoder exception is internal error");
            NoArtifacts(root, "encoder failure has no temporary file");

            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            var retried = capture.Capture(Request(identity, "encoder-retry"), CancellationToken.None).GetAwaiter().GetResult();
            True(retried.Succeeded, "encoder failure releases busy for retry");
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            DeleteRoot(root);
        }
    }

    private static void TestGenerationChangeBeforePublicationCleansTemporaryFile()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-before", "launch-before", "revision-before", 1);
            var generation = 1L;
            var capture = CreateCapture(identity, root, () => generation);
            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) =>
            {
                generation = 2L;
                Thread.Sleep(40);
                return ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
            };

            var failed = capture.Capture(Request(identity, "generation-before"), CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.InputChanged, failed.Error.Code, "generation change during encoding is rejected");
            WaitForWriterCleanup();
            NoArtifacts(root, "pre-publication generation failure has no artifact");

            generation = 1L;
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            True(capture.Capture(Request(identity, "generation-before"), CancellationToken.None).GetAwaiter().GetResult().Succeeded,
                "pre-publication generation failure releases artifact id");
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            DeleteRoot(root);
        }
    }

    private static void TestGenerationChangeAfterPublicationRemovesOwnedArtifact()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-after", "launch-after", "revision-after", 1);
            var reads = 0;
            var release = new ManualResetEventSlim(false);
            var capture = CreateCapture(identity, root, () => Interlocked.Increment(ref reads) >= 8 ? 2L : 1L);
            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) =>
            {
                release.Wait();
                return ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
            };

            var releaseTask = Task.Run(() =>
            {
                while (Volatile.Read(ref reads) < 5) Thread.Sleep(1);
                release.Set();
            });
            var failed = capture.Capture(Request(identity, "generation-after"), CancellationToken.None).GetAwaiter().GetResult();
            releaseTask.GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.InputChanged, failed.Error.Code, "post-publication generation change is rejected");
            WaitForWriterCleanup();
            NoArtifacts(root, "post-publication generation failure removes owned artifact");
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            DeleteRoot(root);
        }
    }

    private static void TestCancellationAndTimeoutDoNotOccupyArtifactId()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-cancel", "launch-cancel", "revision-cancel", 1);
            var capture = CreateCapture(identity, root, () => 1L);
            var encodeStarted = new ManualResetEventSlim(false);
            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) =>
            {
                encodeStarted.Set();
                Thread.Sleep(80);
                return ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
            };
            using (var cancellation = new CancellationTokenSource())
            {
                var cancelTask = Task.Run(() => capture.Capture(Request(identity, "cancel-retry"), cancellation.Token));
                True(encodeStarted.Wait(1000), "cancellable capture reaches asynchronous encoder");
                cancellation.Cancel();
                var cancelled = cancelTask.GetAwaiter().GetResult();
                Equal(RelayLiveLoopErrorCode.Timeout, cancelled.Error.Code, "cancellation maps to timeout");
            }
            WaitForWriterCleanup();
            NoArtifacts(root, "cancelled capture leaves no artifact");

            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) =>
            {
                Thread.Sleep(80);
                return ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
            };
            var timedOut = capture.Capture(Request(identity, "timeout-retry", TimeSpan.FromMilliseconds(10)), CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.Timeout, timedOut.Error.Code, "bounded timeout is enforced during encoding");
            WaitForWriterCleanup();
            NoArtifacts(root, "timed-out capture leaves no artifact");

            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            True(capture.Capture(Request(identity, "cancel-retry"), CancellationToken.None).GetAwaiter().GetResult().Succeeded,
                "cancelled capture releases artifact id");
            True(capture.Capture(Request(identity, "timeout-retry"), CancellationToken.None).GetAwaiter().GetResult().Succeeded,
                "timed-out capture releases artifact id");
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            DeleteRoot(root);
        }
    }

    private static void TestConcurrentCaptureReturnsBusyWithoutLeaking()
    {
        var root = NewRoot();
        try
        {
            var identity = new RuntimeSessionIdentity("session-busy", "launch-busy", "revision-busy", 1);
            var encodeStarted = new ManualResetEventSlim(false);
            var release = new ManualResetEventSlim(false);
            var capture = CreateCapture(identity, root, () => 1L, new AlwaysMainThreadGuard());
            FreshFrameCaptureService.SyntheticEncoderOverride = (width, height, pixels) =>
            {
                encodeStarted.Set();
                release.Wait();
                return ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
            };
            var firstTask = Task.Run(() => capture.Capture(Request(identity, "busy-first"), CancellationToken.None));
            True(encodeStarted.Wait(1000), "first capture reaches asynchronous encoder");
            var busy = capture.Capture(Request(identity, "busy-second"), CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.Busy, busy.Error.Code, "second capture is explicitly busy");
            release.Set();
            True(firstTask.GetAwaiter().GetResult().Succeeded, "first capture completes after busy response");
        }
        finally
        {
            FreshFrameCaptureService.SyntheticEncoderOverride = null;
            DeleteRoot(root);
        }
    }

    private static FreshFrameCaptureService CreateCapture(
        RuntimeSessionIdentity identity, string root, Func<long> generation, IRelayLiveLoopMainThreadGuard guard = null)
    {
        var capture = new FreshFrameCaptureService();
        capture.InitializeOnMainThread(identity, guard ?? new AlwaysMainThreadGuard(), root, generation, 4096);
        return capture;
    }

    private static FreshFrameCaptureRequest Request(RuntimeSessionIdentity identity, string artifactId, TimeSpan? timeout = null)
    {
        Time.Reset(0);
        return new FreshFrameCaptureRequest
        {
            ArtifactId = artifactId,
            ExpectedSessionId = identity.SessionId,
            ExpectedRuntimeRevision = identity.RuntimeRevision,
            ExpectedViewportGeneration = 1,
            MinimumFrameExclusive = 0,
            MaximumWidth = 64,
            MaximumHeight = 64,
            Timeout = timeout ?? TimeSpan.FromSeconds(2)
        };
    }

    private static string NewRoot()
    {
        var root = Path.Combine(Path.GetTempPath(), "relay-liveloop-fresh-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        return root;
    }

    private static void WaitForWriterCleanup()
    {
        Thread.Sleep(150);
    }

    private static void NoArtifacts(string root, string message)
    {
        WaitForWriterCleanup();
        Equal(0, Directory.GetFiles(root, "*", SearchOption.TopDirectoryOnly).Length, message);
    }

    private static void DeleteRoot(string root)
    {
        if (Directory.Exists(root)) Directory.Delete(root, true);
    }

    private static void True(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
        _passed++;
    }

    private static void Equal<T>(T expected, T actual, string message)
    {
        if (!object.Equals(expected, actual)) throw new InvalidOperationException(message + ": expected=" + expected + " actual=" + actual);
        _passed++;
    }

    private sealed class AlwaysMainThreadGuard : IRelayLiveLoopMainThreadGuard
    {
        public bool IsMainThread { get { return true; } }
        public void AssertMainThread() { }
    }
}
#endif
