#if UNITY_EDITOR
using System;
using UnityEditor;

namespace RelayLiveLoop
{
    [InitializeOnLoad]
    public static class RelayLiveLoopEditorWorkerBootstrap
    {
        private const string JobRootSessionKey = "RelayLiveLoop.EditorJobRoot";
        private const string ArtifactRootSessionKey = "RelayLiveLoop.EditorArtifactRoot";
        private const string JobRootEnvironmentKey = "RELAYLIVELOOP_EDITOR_JOB_ROOT";
        private const string ArtifactRootEnvironmentKey = "RELAYLIVELOOP_ARTIFACT_ROOT";
        private static readonly EditorJobProviderRegistry Providers = new EditorJobProviderRegistry();
        private static RelayLiveLoopEditorWorker _worker;
        private static string _configuredJobRoot;

        static RelayLiveLoopEditorWorkerBootstrap()
        {
            AssemblyReloadEvents.beforeAssemblyReload += BeforeAssemblyReload;
            TryRestoreConfiguration();
        }

        public static string ConfiguredJobRoot { get { return _worker == null ? null : _configuredJobRoot; } }

        public static bool IsConfigured { get { return _worker != null; } }

        public static IDisposable RegisterProvider(IEditorJobProvider provider)
        {
            return Providers.Register(provider);
        }

        public static void ConfigureForCurrentEditorSession(string jobRoot, string artifactRoot)
        {
            if (string.IsNullOrWhiteSpace(jobRoot)) throw new ArgumentException("Job root is required.", nameof(jobRoot));
            if (string.IsNullOrWhiteSpace(artifactRoot)) throw new ArgumentException("Artifact root is required.", nameof(artifactRoot));
            SessionState.SetString(JobRootSessionKey, jobRoot);
            SessionState.SetString(ArtifactRootSessionKey, artifactRoot);
            Configure(jobRoot, artifactRoot);
        }

        public static void StopForCurrentEditorSession()
        {
            EditorApplication.update -= Tick;
            _worker = null;
            _configuredJobRoot = null;
            SessionState.EraseString(JobRootSessionKey);
            SessionState.EraseString(ArtifactRootSessionKey);
        }

        private static void TryRestoreConfiguration()
        {
            var jobRoot = SessionState.GetString(JobRootSessionKey, string.Empty);
            var artifactRoot = SessionState.GetString(ArtifactRootSessionKey, string.Empty);
            if (string.IsNullOrWhiteSpace(jobRoot))
            {
                jobRoot = Environment.GetEnvironmentVariable(JobRootEnvironmentKey);
            }

            if (string.IsNullOrWhiteSpace(artifactRoot))
            {
                artifactRoot = Environment.GetEnvironmentVariable(ArtifactRootEnvironmentKey);
            }

            if (!string.IsNullOrWhiteSpace(jobRoot) && !string.IsNullOrWhiteSpace(artifactRoot))
            {
                SessionState.SetString(JobRootSessionKey, jobRoot);
                SessionState.SetString(ArtifactRootSessionKey, artifactRoot);
                Configure(jobRoot, artifactRoot);
            }
        }

        private static void Configure(string jobRoot, string artifactRoot)
        {
            EditorApplication.update -= Tick;
            _worker = new RelayLiveLoopEditorWorker(
                new AtomicEditorJobStore(jobRoot, artifactRoot),
                Providers);
            _configuredJobRoot = System.IO.Path.GetFullPath(jobRoot);
            EditorApplication.update += Tick;
        }

        private static void Tick()
        {
            if (_worker != null) _worker.Tick();
        }

        private static void BeforeAssemblyReload()
        {
            EditorApplication.update -= Tick;
            _worker = null;
            _configuredJobRoot = null;
            Providers.Clear();
        }
    }
}
#endif
