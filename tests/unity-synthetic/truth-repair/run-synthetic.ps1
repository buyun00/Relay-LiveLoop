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
if (!(Test-Path -LiteralPath $mono -PathType Leaf) -or !(Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'Unity-bundled Mono or Roslyn compiler was not found.'
}

$testRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $testRoot))
$packageRoot = Join-Path $repositoryRoot 'unity-package'
if (!(Test-Path -LiteralPath (Join-Path $packageRoot 'package.json') -PathType Leaf)) {
    throw 'Relay LiveLoop package root was not found.'
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
    $runtimeSources = @(Get-ChildItem -LiteralPath (Join-Path $packageRoot 'Runtime\Core') -File -Filter '*.cs' | Select-Object -ExpandProperty FullName)
    $editorSources = @(Get-ChildItem -LiteralPath (Join-Path $packageRoot 'Editor\Core') -File -Filter '*.cs' | Select-Object -ExpandProperty FullName)
    $unityStubs = Join-Path $repositoryRoot 'tests\unity-synthetic\observation-preview\UnityStubs.cs'
    $editorStubs = Join-Path $repositoryRoot 'tests\unity-synthetic\shared\EditorSyntheticStubs.cs'
    $testSource = Join-Path $testRoot 'SyntheticRepairTests.cs'
    $testExe = Join-Path $outputRoot 'RelayLiveLoop.TruthRepairSyntheticTests.exe'
    $arguments = @(
        '/nologo',
        '/langversion:9.0',
        '/define:UNITY_EDITOR,DEVELOPMENT_BUILD,RELAYLIVELOOP_SYNTHETIC',
        '/target:exe',
        "/out:$testExe",
        '/r:System.Web.Extensions.dll'
    ) + $runtimeSources + $editorSources + @($unityStubs, $editorStubs, $testSource)
    & $mono $compiler $arguments
    if ($LASTEXITCODE -ne 0) { throw "Synthetic compile failed with exit code $LASTEXITCODE." }
    & $mono $testExe
    if ($LASTEXITCODE -ne 0) { throw "Synthetic tests failed with exit code $LASTEXITCODE." }

    $managedRoot = Join-Path $UnityRoot 'Editor\Data\Managed'
    $netStandardRoot = Join-Path $UnityRoot 'Editor\Data\NetStandard'
    $netFxShimRoot = Join-Path $netStandardRoot 'compat\2.1.0\shims\netfx'
    $referenceDll = Join-Path $outputRoot 'RelayLiveLoop.TruthRepair.UnityCompileCheck.dll'
    $referenceArguments = @(
        '/nologo',
        '/nostdlib+',
        '/langversion:9.0',
        '/define:UNITY_EDITOR,DEVELOPMENT_BUILD',
        '/target:library',
        "/out:$referenceDll",
        "/r:$(Join-Path $netFxShimRoot 'mscorlib.dll')",
        "/r:$(Join-Path $netStandardRoot 'ref\2.1.0\netstandard.dll')",
        "/r:$(Join-Path $netFxShimRoot 'System.dll')",
        "/r:$(Join-Path $netFxShimRoot 'System.Core.dll')",
        "/r:$(Join-Path $managedRoot 'UnityEngine\UnityEngine.CoreModule.dll')",
        "/r:$(Join-Path $managedRoot 'UnityEngine\UnityEngine.JSONSerializeModule.dll')",
        "/r:$(Join-Path $managedRoot 'UnityEngine\UnityEngine.ScreenCaptureModule.dll')",
        "/r:$(Join-Path $managedRoot 'UnityEngine\UnityEditor.CoreModule.dll')"
    ) + $runtimeSources + $editorSources
    & $mono $compiler $referenceArguments
    if ($LASTEXITCODE -ne 0) { throw "Unity 2022.3 reference compile failed with exit code $LASTEXITCODE." }
    Write-Output 'UNITY REFERENCE COMPILE PASS: current neutral Runtime/Core and Editor/Core.'

    $releaseDll = Join-Path $outputRoot 'RelayLiveLoop.TruthRepair.ReleaseSurface.dll'
    & $mono $compiler '/nologo' '/langversion:9.0' '/target:library' "/out:$releaseDll" $runtimeSources
    if ($LASTEXITCODE -ne 0) { throw "Release-isolation compile failed with exit code $LASTEXITCODE." }
    $inspectorExe = Join-Path $outputRoot 'ReleaseSurfaceInspector.exe'
    $inspectorSource = Join-Path $repositoryRoot 'tests\unity-synthetic\runtime-worker\ReleaseSurfaceInspector.cs'
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:RELAYLIVELOOP_RELEASE_INSPECTOR' '/target:exe' "/out:$inspectorExe" $inspectorSource
    if ($LASTEXITCODE -ne 0) { throw "Release inspector compile failed with exit code $LASTEXITCODE." }
    & $mono $inspectorExe $releaseDll
    if ($LASTEXITCODE -ne 0) { throw 'Release-isolation check found an exported Runtime control type.' }
}
finally {
    if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
}
