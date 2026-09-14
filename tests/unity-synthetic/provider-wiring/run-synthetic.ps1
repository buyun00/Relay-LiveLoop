param(
    [string]$UnityRoot
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($UnityRoot)) {
    $candidate = Get-ChildItem -LiteralPath $env:ProgramFiles -Directory -Filter 'Unity 2022.3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($null -ne $candidate) { $UnityRoot = $candidate.FullName }
}
if ([string]::IsNullOrWhiteSpace($UnityRoot)) { throw 'Unity 2022.3 root was not supplied or discovered.' }

$mono = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\bin\mono.exe'
$compiler = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\lib\mono\msbuild\Current\bin\Roslyn\csc.exe'
if (!(Test-Path -LiteralPath $mono) -or !(Test-Path -LiteralPath $compiler)) {
    throw 'Unity-bundled Mono or Roslyn compiler is missing.'
}

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $testRoot))
$packageRoot = Join-Path $repositoryRoot 'unity-package'
if (!(Test-Path -LiteralPath (Join-Path $packageRoot 'package.json'))) {
    throw 'Relay LiveLoop package root was not found from the formal test location.'
}
$resolvedTestRoot = [IO.Path]::GetFullPath($testRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$outputRoot = [IO.Path]::GetFullPath((Join-Path $resolvedTestRoot '.out'))
$expectedPrefix = $resolvedTestRoot + [IO.Path]::DirectorySeparatorChar
if (!$outputRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Synthetic output directory escaped the test root.'
}
if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
New-Item -ItemType Directory -Path $outputRoot | Out-Null

try {
    $baseRuntimeNames = @(
        'BoundedMessageFramer.cs',
        'BridgePrimitives.cs',
        'MainThreadDispatcher.cs',
        'ObjectHandleRegistry.cs',
        'ProviderRegistry.cs',
        'RuntimeBridgeCore.cs',
        'SerializedContextEnvelope.cs',
        'SessionAuthentication.cs'
    )
    $baseEditorNames = @(
        'AtomicEditorJobStore.cs',
        'EditorJobContracts.cs',
        'EditorJobProviderRegistry.cs',
        'RelayLiveLoopEditorWorker.cs',
        'RelayLiveLoopEditorWorkerBootstrap.cs'
    )
    $runtimeNames = $baseRuntimeNames + @('NeutralProviderContracts.cs', 'RuntimeProviderHub.cs')
    $editorNames = $baseEditorNames + @('NeutralEditorProviderContracts.cs', 'NeutralEditorProviderHub.cs')
    $runtimeSources = @($runtimeNames | ForEach-Object { Join-Path $packageRoot "Runtime\Core\$_" })
    $editorSources = @($editorNames | ForEach-Object { Join-Path $packageRoot "Editor\Core\$_" })
    $unityStubs = Join-Path $repositoryRoot 'tests\unity-synthetic\runtime-worker\UnityStubs.cs'
    $testSources = @($unityStubs, (Join-Path $testRoot 'SyntheticTests.cs'), (Join-Path $testRoot 'ReleaseSurfaceInspector.cs'))
    $testExe = Join-Path $outputRoot 'RelayLiveLoop.ProviderSyntheticTests.exe'
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:UNITY_EDITOR,DEVELOPMENT_BUILD,RELAYLIVELOOP_SYNTHETIC' '/target:exe' "/out:$testExe" '/r:System.Web.Extensions.dll' $runtimeSources $editorSources $testSources
    if ($LASTEXITCODE -ne 0) { throw "Synthetic compile failed with exit code $LASTEXITCODE." }
    & $mono $testExe
    if ($LASTEXITCODE -ne 0) { throw "Synthetic tests failed with exit code $LASTEXITCODE." }

    $unityEngine = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine\UnityEngine.CoreModule.dll'
    $unityJson = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine\UnityEngine.JSONSerializeModule.dll'
    $unityEditor = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine\UnityEditor.CoreModule.dll'
    $netStandard = Join-Path $UnityRoot 'Editor\Data\NetStandard\ref\2.1.0\netstandard.dll'
    $netFxShimRoot = Join-Path $UnityRoot 'Editor\Data\NetStandard\compat\2.1.0\shims\netfx'
    $mscorlib = Join-Path $netFxShimRoot 'mscorlib.dll'
    $system = Join-Path $netFxShimRoot 'System.dll'
    $systemCore = Join-Path $netFxShimRoot 'System.Core.dll'
    $unityCompileDll = Join-Path $outputRoot 'RelayLiveLoop.ProviderUnityCompileCheck.dll'
    & $mono $compiler '/nologo' '/nostdlib+' '/langversion:9.0' '/define:UNITY_EDITOR,DEVELOPMENT_BUILD' '/target:library' "/out:$unityCompileDll" "/r:$mscorlib" "/r:$netStandard" "/r:$system" "/r:$systemCore" "/r:$unityEngine" "/r:$unityJson" "/r:$unityEditor" $runtimeSources $editorSources
    if ($LASTEXITCODE -ne 0) { throw "Unity reference compile failed with exit code $LASTEXITCODE." }
    Write-Output 'UNITY REFERENCE COMPILE PASS: neutral Runtime and Editor provider wiring against Unity 2022.3 managed assemblies.'

    $releaseDll = Join-Path $outputRoot 'RelayLiveLoop.ProviderReleaseSurface.dll'
    & $mono $compiler '/nologo' '/langversion:9.0' '/target:library' "/out:$releaseDll" $runtimeSources
    if ($LASTEXITCODE -ne 0) { throw "Release-isolation compile failed with exit code $LASTEXITCODE." }
    $inspectorExe = Join-Path $outputRoot 'ReleaseSurfaceInspector.exe'
    $inspectorSource = Join-Path $testRoot 'ReleaseSurfaceInspector.cs'
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:RELAYLIVELOOP_RELEASE_INSPECTOR' '/target:exe' "/out:$inspectorExe" $inspectorSource
    if ($LASTEXITCODE -ne 0) { throw "Release inspector compile failed with exit code $LASTEXITCODE." }
    & $mono $inspectorExe $releaseDll
    if ($LASTEXITCODE -ne 0) { throw 'Release-isolation check found an exported runtime control type.' }
}
finally {
    if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
}
