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
            TestObservationAndPreviewRevert();
            TestChangedPreviewIsNotOverwritten();
            TestAbsentProviderIsExplicit();
            TestFreshFrameCaptureAndPng();
            RunPngBenchmark();
            Console.WriteLine("SYNTHETIC PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SYNTHETIC FAIL: " + ex);
            return 1;
        }
    }

    private static void RunPngBenchmark()
    {
        const int width = 512;
        const int height = 512;
        const int iterations = 3;
        var pixels = new Color32[width * height];
        for (var index = 0; index < pixels.Length; index++)
        {
            pixels[index] = new Color32((byte)(index & 0xff), (byte)((index >> 4) & 0xff), 127, 255);
        }
        var stopwatch = Stopwatch.StartNew();
        byte[] encoded = null;
        for (var iteration = 0; iteration < iterations; iteration++)
        {
            encoded = ManagedPngEncoder.EncodeRgba32(width, height, pixels, true);
        }
        stopwatch.Stop();
        Console.WriteLine(
            "BENCHMARK synthetic png width=" + width + " height=" + height + " iterations=" + iterations +
            " elapsed_ms=" + stopwatch.Elapsed.TotalMilliseconds.ToString("F2") + " bytes=" + encoded.Length);
    }

    private static void TestObservationAndPreviewRevert()
    {
        Time.Reset(50);
        var fixture = CreateFixture();
        var observed = fixture.Observation.ObserveObject(fixture.GameObjectHandle, 16);
        True(observed.Succeeded, "object observation succeeds on main thread");
        Equal("root/child", observed.Value.HierarchyPath, "bounded hierarchy path captured");
        Equal(1, observed.Value.Components.Count, "component list captured");
        var rectHandle = observed.Value.Components[0].Handle;
        var property = fixture.Observation.ObserveProperty(rectHandle, "anchoredPosition");
        True(property.Succeeded, "built-in RectTransform property observed");
        True(property.Value.Value.EquivalentTo(ComponentValue.Vector2(0, 0)), "initial property value read");

        var preview = fixture.Preview.Preview(new ComponentPreviewRequest
        {
            TaskId = "task-a",
            Component = rectHandle,
            Property = "anchoredPosition",
            Expected = ComponentValue.Vector2(0, 0),
            Replacement = ComponentValue.Vector2(12, 24)
        });
        True(preview.Succeeded, "component preview applies expected value");
        True(preview.Value.Readback.EquivalentTo(ComponentValue.Vector2(12, 24)), "component preview readback matches");
        True(!preview.Value.FormalSourceChanged, "preview is marked non-formal");

        var reverted = fixture.Preview.Revert("task-a", preview.Value.OverlayId);
        True(reverted.Succeeded, "component preview reverts");
        True(reverted.Value.Readback.EquivalentTo(ComponentValue.Vector2(0, 0)), "component revert readback matches original");

        fixture.Handles.AdvanceGeneration("view-a");
        Equal(
            RelayLiveLoopErrorCode.StaleTarget,
            fixture.Observation.ObserveObject(fixture.GameObjectHandle, 16).Error.Code,
            "old observation handle rejected after view generation advances");
    }

    private static void TestChangedPreviewIsNotOverwritten()
    {
        var fixture = CreateFixture();
        var rectHandle = fixture.Observation.ObserveObject(fixture.GameObjectHandle, 16).Value.Components[0].Handle;
        var preview = fixture.Preview.Preview(new ComponentPreviewRequest
        {
            TaskId = "task-b",
            Component = rectHandle,
            Property = "anchoredPosition",
            Expected = ComponentValue.Vector2(0, 0),
            Replacement = ComponentValue.Vector2(5, 6)
        }).Value;
        fixture.Rect.anchoredPosition = new Vector2(7, 8);
        var revert = fixture.Preview.Revert("task-b", preview.OverlayId);
        Equal(RelayLiveLoopErrorCode.InputChanged, revert.Error.Code, "revert refuses a value changed after preview");
        Equal(7f, fixture.Rect.anchoredPosition.x, "conflicting value remains untouched");

        var unsupported = fixture.Observation.ObserveProperty(rectHandle, "unregisteredProperty");
        Equal(RelayLiveLoopErrorCode.CapabilityUnavailable, unsupported.Error.Code, "unsupported component property is explicit");
    }

    private static void TestAbsentProviderIsExplicit()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var registry = new ProviderRegistry(dispatcher);
        var result = registry.Resolve<IRuntimeObservationProvider>(
            RelayLiveLoopProviderKind.Observation,
            "missing",
            "stable",
            0);
        Equal(RelayLiveLoopErrorCode.CapabilityUnavailable, result.Error.Code, "absent provider is not mocked as available");
    }

    private static void TestFreshFrameCaptureAndPng()
    {
        var root = Path.Combine(Path.GetTempPath(), "relay-liveloop-frame-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(root);
            Time.Reset(100);
            var dispatcher = new RelayLiveLoopMainThreadDispatcher();
            dispatcher.InitializeOnMainThread();
            var identity = new RuntimeSessionIdentity("session-frame", "launch-frame", "revision-frame", 1);
            var capture = new FreshFrameCaptureService();
            capture.InitializeOnMainThread(identity, dispatcher, root, 4096);
            var request = new FreshFrameCaptureRequest
            {
                ArtifactId = "frame-a",
                ExpectedSessionId = identity.SessionId,
                ExpectedRuntimeRevision = identity.RuntimeRevision,
                MinimumFrameExclusive = 100,
                MaximumWidth = 64,
                MaximumHeight = 64,
                Timeout = TimeSpan.FromSeconds(2)
            };
            var result = capture.Capture(request, CancellationToken.None).GetAwaiter().GetResult();
            True(result.Succeeded, "fresh frame capture succeeds");
            True(result.Value.Fresh && result.Value.Frame > 100, "captured frame is newer than baseline");
            Equal(64, result.Value.Sha256.Length, "frame artifact has sha256");
            var png = File.ReadAllBytes(result.Value.Path);
            ValidatePng(png, 2, 2);

            var duplicate = capture.Capture(request, CancellationToken.None).GetAwaiter().GetResult();
            Equal(RelayLiveLoopErrorCode.InputChanged, duplicate.Error.Code, "existing screenshot artifact is not overwritten");
            request.ArtifactId = "frame-b";
            request.ExpectedSessionId = "wrong-session";
            Equal(
                RelayLiveLoopErrorCode.WrongSession,
                capture.Capture(request, CancellationToken.None).GetAwaiter().GetResult().Error.Code,
                "frame capture rejects wrong session");
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    private static Fixture CreateFixture()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var identity = new RuntimeSessionIdentity("session-a", "launch-a", "revision-a", 1);
        var handles = new ObjectHandleRegistry(identity, dispatcher);
        var root = new GameObject("root", typeof(RectTransform));
        var child = new GameObject("child", typeof(RectTransform));
        child.transform.parent = root.transform;
        var gameObjectHandle = handles.Register("view-a", child);
        return new Fixture
        {
            Handles = handles,
            GameObjectHandle = gameObjectHandle,
            Rect = (RectTransform)child.transform,
            Observation = new MainThreadObservationService(identity, dispatcher, handles),
            Preview = new MainThreadComponentPreviewService(dispatcher, handles)
        };
    }

    private static void ValidatePng(byte[] png, int expectedWidth, int expectedHeight)
    {
        var signature = new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 };
        for (var index = 0; index < signature.Length; index++) Equal(signature[index], png[index], "PNG signature byte");
        Equal((uint)expectedWidth, ReadBigEndian(png, 16), "PNG width");
        Equal((uint)expectedHeight, ReadBigEndian(png, 20), "PNG height");
        var offset = 8;
        var sawIdat = false;
        var sawIend = false;
        while (offset < png.Length)
        {
            var length = (int)ReadBigEndian(png, offset);
            var type = Encoding.ASCII.GetString(png, offset + 4, 4);
            var dataOffset = offset + 8;
            var expectedCrc = ReadBigEndian(png, dataOffset + length);
            Equal(expectedCrc, Crc32(png, offset + 4, length + 4), "PNG chunk CRC");
            if (type == "IDAT")
            {
                sawIdat = true;
                Equal((byte)0x78, png[dataOffset], "zlib CMF");
                Equal((byte)0x01, png[dataOffset + 1], "zlib FLG");
            }
            if (type == "IEND") sawIend = true;
            offset = dataOffset + length + 4;
        }
        True(sawIdat && sawIend, "PNG contains IDAT and IEND");
    }

    private static readonly uint[] CrcTable = BuildCrcTable();

    private static uint Crc32(byte[] bytes, int offset, int count)
    {
        var crc = 0xffffffffu;
        for (var index = 0; index < count; index++) crc = CrcTable[(crc ^ bytes[offset + index]) & 0xff] ^ (crc >> 8);
        return crc ^ 0xffffffffu;
    }

    private static uint[] BuildCrcTable()
    {
        var table = new uint[256];
        for (uint index = 0; index < table.Length; index++)
        {
            var value = index;
            for (var bit = 0; bit < 8; bit++) value = (value & 1) != 0 ? 0xedb88320u ^ (value >> 1) : value >> 1;
            table[index] = value;
        }
        return table;
    }

    private static uint ReadBigEndian(byte[] bytes, int offset)
    {
        return ((uint)bytes[offset] << 24) | ((uint)bytes[offset + 1] << 16) |
            ((uint)bytes[offset + 2] << 8) | bytes[offset + 3];
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

    private sealed class Fixture
    {
        public ObjectHandleRegistry Handles;
        public RuntimeObjectHandle GameObjectHandle;
        public RectTransform Rect;
        public MainThreadObservationService Observation;
        public MainThreadComponentPreviewService Preview;
    }
}
#endif
