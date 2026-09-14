#if UNITY_EDITOR
using System;
using System.Collections.Generic;

namespace RelayLiveLoop
{
    [Serializable]
    public sealed class CompileJobPayload
    {
        public string buildTarget;
        public string configuration;
        public List<string> defines = new List<string>();
        public List<string> references = new List<string>();
        public List<string> sourceInputs = new List<string>();
    }

    [Serializable]
    public sealed class AssetBuildJobPayload
    {
        public string packageId;
        public string buildProfileId;
        public List<string> affectedAssetIds = new List<string>();
        public string optionsJson;
    }

    [Serializable]
    public sealed class SourceJobPayload
    {
        public string sourceGuid;
        public long localId;
        public string propertyPath;
        public string expectedValueJson;
        public string replacementValueJson;
    }

    public interface ICompileJobProvider
    {
        IEditorJobOperation BeginCompile(CompileJobPayload payload, EditorJobExecution execution);
        IEditorJobOperation RecoverCompile(CompileJobPayload payload, EditorJobExecution execution);
    }

    public interface IAssetBuildJobProvider
    {
        IEditorJobOperation BeginAssetBuild(AssetBuildJobPayload payload, EditorJobExecution execution);
        IEditorJobOperation RecoverAssetBuild(AssetBuildJobPayload payload, EditorJobExecution execution);
    }

    public interface ISourceJobProvider
    {
        IEditorJobOperation BeginLocate(SourceJobPayload payload, EditorJobExecution execution);
        IEditorJobOperation RecoverLocate(SourceJobPayload payload, EditorJobExecution execution);
        IEditorJobOperation BeginEdit(SourceJobPayload payload, EditorJobExecution execution);
        IEditorJobOperation RecoverEdit(SourceJobPayload payload, EditorJobExecution execution);
    }
}
#endif
