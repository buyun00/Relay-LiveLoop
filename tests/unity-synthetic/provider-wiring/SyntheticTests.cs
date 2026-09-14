#if RELAYLIVELOOP_SYNTHETIC
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
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
            TestRuntimeProviderResolutionAndGeneration();
            TestModuleContextAndExceptionEvidence();
            TestMissingEditorAdapterReturnsCapabilityUnavailable();
            TestCompileAdapterProducesSealedArtifact();
            RunProviderRoutingBenchmark();
            Console.WriteLine("SYNTHETIC PASS: " + _passed + " checks");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SYNTHETIC FAIL: " + ex);
            return 1;
        }
    }

    private static void RunProviderRoutingBenchmark()
    {
        const int iterations = 20000;
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var registry = new ProviderRegistry(dispatcher);
        var hub = new RuntimeProviderHub(dispatcher, registry);
        registry.Register<IPageProvider>(RelayLiveLoopProviderKind.Page, "bench-page", "bench-owner", 0, new FakePageProvider());
        var address = new ProviderAddress
        {
            Kind = RelayLiveLoopProviderKind.Page,
            ProviderId = "bench-page",
            OwnerId = "bench-owner",
            OwnerGeneration = 0
        };
        var request = new PageOperationRequest { TaskId = "bench-task", PageId = "bench-page", ExpectedViewGeneration = 2 };
        var stopwatch = Stopwatch.StartNew();
        for (var index = 0; index < iterations; index++)
        {
            if (!hub.ObservePage(address, request).Succeeded) throw new InvalidOperationException("Provider benchmark failed.");
        }
        stopwatch.Stop();
        Console.WriteLine("BENCHMARK synthetic provider_routes=" + iterations + " elapsed_ms=" + stopwatch.Elapsed.TotalMilliseconds.ToString("F2"));
    }

    private static void TestRuntimeProviderResolutionAndGeneration()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var registry = new ProviderRegistry(dispatcher);
        var hub = new RuntimeProviderHub(dispatcher, registry);
        var address = new ProviderAddress
        {
            Kind = RelayLiveLoopProviderKind.Page,
            ProviderId = "page-a",
            OwnerId = "module-a",
            OwnerGeneration = 0
        };
        var request = new PageOperationRequest { TaskId = "task-a", PageId = "page-a", ExpectedViewGeneration = 2 };
        Equal(
            RelayLiveLoopErrorCode.CapabilityUnavailable,
            hub.ObservePage(address, request).Error.Code,
            "missing page provider is explicit");

        registry.Register<IPageProvider>(RelayLiveLoopProviderKind.Page, "page-a", "module-a", 0, new FakePageProvider());
        var observed = hub.ObservePage(address, request);
        True(observed.Succeeded, "registered page provider invoked");
        Equal(2L, observed.Value.Value.ViewGeneration, "page generation returned");
        Equal(1, observed.Value.Evidence.Count, "page evidence preserved");
        hub.AdvanceProviderOwnerGeneration("module-a");
        Equal(
            RelayLiveLoopErrorCode.StaleTarget,
            hub.ObservePage(address, request).Error.Code,
            "old page provider generation rejected");
    }

    private static void TestModuleContextAndExceptionEvidence()
    {
        var dispatcher = new RelayLiveLoopMainThreadDispatcher();
        dispatcher.InitializeOnMainThread();
        var registry = new ProviderRegistry(dispatcher);
        var hub = new RuntimeProviderHub(dispatcher, registry);
        registry.Register<IModuleProvider>(RelayLiveLoopProviderKind.Module, "module-a", "module-owner", 0, new FakeModuleProvider(false));
        var address = new ProviderAddress
        {
            Kind = RelayLiveLoopProviderKind.Module,
            ProviderId = "module-a",
            OwnerId = "module-owner",
            OwnerGeneration = 0
        };
        var request = new ModuleOperationRequest
        {
            TaskId = "task-b",
            ModuleId = "module-a",
            ExpectedModuleGeneration = 0,
            DependencyClosure = new[] { "module-a" }
        };
        var context = hub.CaptureModuleContext(address, request);
        True(context.Succeeded, "module context captured as serialized bytes");
        Equal("schema.synthetic", context.Value.Value.SchemaId, "module context schema preserved");

        registry.Register<IModuleProvider>(RelayLiveLoopProviderKind.Module, "module-b", "module-owner-b", 0, new FakeModuleProvider(true));
        address.ProviderId = "module-b";
        address.OwnerId = "module-owner-b";
        var failed = hub.QuiesceModule(address, request);
        Equal(RelayLiveLoopErrorCode.InternalError, failed.Error.Code, "provider exception becomes structured error");
        Equal("module.quiesce", failed.Error.Stage, "provider exception stage preserved");
    }

    private static void TestMissingEditorAdapterReturnsCapabilityUnavailable()
    {
        WithTemporaryRoots((jobRoot, artifactRoot) =>
        {
            var store = new AtomicEditorJobStore(jobRoot, artifactRoot);
            var providers = new EditorJobProviderRegistry();
            var hub = new NeutralEditorProviderHub("neutral-editor");
            providers.Register(hub);
            WriteCompileRequest(store.IncomingDirectory, "job-missing", artifactRoot);
            var worker = new RelayLiveLoopEditorWorker(store, providers);
            PumpUntilResult(worker, jobRoot, "job-missing");
            var result = ReadResult(jobRoot, "job-missing");
            Equal("failed", result.status, "missing editor adapter fails job");
            Equal("CAPABILITY_UNAVAILABLE", result.error.code, "missing editor adapter reports capability unavailable");
            Equal(0, result.artifacts.Count, "missing editor adapter produces no fake artifact");
        });
    }

    private static void TestCompileAdapterProducesSealedArtifact()
    {
        WithTemporaryRoots((jobRoot, artifactRoot) =>
        {
            var store = new AtomicEditorJobStore(jobRoot, artifactRoot);
            var providers = new EditorJobProviderRegistry();
            var hub = new NeutralEditorProviderHub("neutral-editor");
            hub.RegisterCompile(new FakeCompileProvider());
            providers.Register(hub);
            WriteCompileRequest(store.IncomingDirectory, "job-compile", artifactRoot);
            var worker = new RelayLiveLoopEditorWorker(store, providers);
            PumpUntilResult(worker, jobRoot, "job-compile");
            var result = ReadResult(jobRoot, "job-compile");
            Equal("completed", result.status, "registered compile adapter completes");
            Equal(1, result.artifacts.Count, "registered compile adapter artifact sealed");
            Equal(64, result.artifacts[0].sha256.Length, "compile artifact has sha256");
        });
    }

    private static void WriteCompileRequest(string incoming, string jobId, string artifactRoot)
    {
        var payload = new CompileJobPayload
        {
            buildTarget = "SyntheticTarget",
            configuration = "Development",
            sourceInputs = new List<string> { "Synthetic.cs" }
        };
        var request = new EditorJobRequest
        {
            jobId = jobId,
            providerId = "neutral-editor",
            kind = "compile",
            inputSnapshot = "snapshot-" + jobId,
            artifactRoot = artifactRoot,
            requestedAtUtc = DateTimeOffset.UtcNow.ToString("o"),
            expiresAtUtc = DateTimeOffset.UtcNow.AddMinutes(2).ToString("o"),
            payloadJson = JsonUtility.ToJson(payload, true)
        };
        File.WriteAllText(Path.Combine(incoming, jobId + ".request.json"), JsonUtility.ToJson(request, true));
    }

    private static EditorJobResult ReadResult(string jobRoot, string jobId)
    {
        return JsonUtility.FromJson<EditorJobResult>(File.ReadAllText(Path.Combine(jobRoot, "results", jobId + ".result.json")));
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
        var root = Path.Combine(Path.GetTempPath(), "relay-liveloop-provider-" + Guid.NewGuid().ToString("N"));
        try
        {
            test(Path.Combine(root, "jobs"), Path.Combine(root, "artifacts"));
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    private static ProviderReply<PageRuntimeState> PageReply()
    {
        return new ProviderReply<PageRuntimeState>(
            new PageRuntimeState
            {
                PageId = "page-a",
                ModuleId = "module-a",
                ModuleGeneration = 0,
                ViewGeneration = 2,
                Stable = true,
                Frame = 1
            },
            false,
            new[]
            {
                new ProviderEvidence
                {
                    Kind = "state",
                    Stage = "observe",
                    ArtifactId = "artifact-a",
                    Sha256 = new string('a', 64)
                }
            });
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

    private sealed class FakePageProvider : IPageProvider
    {
        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Observe(PageOperationRequest request) { return RelayLiveLoopResult<ProviderReply<PageRuntimeState>>.Success(PageReply()); }
        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Refresh(PageOperationRequest request) { return Observe(request); }
        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(PageOperationRequest request) { throw new NotSupportedException(); }
        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> RestoreContext(PageOperationRequest request, SerializedContextEnvelope context) { return Observe(request); }
        public RelayLiveLoopResult<ProviderReply<PageRuntimeState>> Rebuild(PageOperationRequest request) { return Observe(request); }
    }

    private sealed class FakeModuleProvider : IModuleProvider
    {
        private readonly bool _throwOnQuiesce;
        public FakeModuleProvider(bool throwOnQuiesce) { _throwOnQuiesce = throwOnQuiesce; }
        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Observe(ModuleOperationRequest request) { return State("observed"); }
        public RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>> CaptureContext(ModuleOperationRequest request)
        {
            var context = new SerializedContextEnvelope(
                "schema.synthetic", 1, "application/json", "session-a", 0, 0, "revision-a", new byte[] { 123, 125 });
            return RelayLiveLoopResult<ProviderReply<SerializedContextEnvelope>>.Success(
                new ProviderReply<SerializedContextEnvelope>(context, false, Array.Empty<ProviderEvidence>()));
        }
        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Quiesce(ModuleOperationRequest request)
        {
            if (_throwOnQuiesce) throw new InvalidOperationException("synthetic failure");
            return State("quiesced");
        }
        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Dispose(ModuleOperationRequest request) { return State("disposed"); }
        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Load(ModuleOperationRequest request) { return State("loaded"); }
        public RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> Restore(ModuleOperationRequest request, SerializedContextEnvelope context) { return State("restored"); }
        private static RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>> State(string stage)
        {
            return RelayLiveLoopResult<ProviderReply<ModuleRuntimeState>>.Success(
                new ProviderReply<ModuleRuntimeState>(
                    new ModuleRuntimeState { ModuleId = "module-a", ModuleGeneration = 0, Stage = stage },
                    false,
                    Array.Empty<ProviderEvidence>()));
        }
    }

    private sealed class FakeCompileProvider : ICompileJobProvider
    {
        public IEditorJobOperation BeginCompile(CompileJobPayload payload, EditorJobExecution execution)
        {
            var path = Path.Combine(execution.Attempt.attemptRoot, "Synthetic.dll");
            File.WriteAllText(path, "synthetic compile output");
            return new CompleteOperation(path);
        }
        public IEditorJobOperation RecoverCompile(CompileJobPayload payload, EditorJobExecution execution) { return null; }
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
                        artifactId = "compile-a",
                        kind = "assembly",
                        path = _path,
                        mediaType = "application/octet-stream"
                    }
                });
        }
    }
}
#endif
