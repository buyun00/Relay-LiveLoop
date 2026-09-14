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
if (!(Test-Path -LiteralPath (Join-Path $packageRoot 'package.json') -PathType Leaf)) {
    throw 'Relay LiveLoop Unity package root was not found.'
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
    $testSources = @(Get-ChildItem -LiteralPath $testRoot -File -Filter '*.cs' | Select-Object -ExpandProperty FullName)
    $testExe = Join-Path $outputRoot 'RelayLiveLoop.ObservationSyntheticTests.exe'
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:UNITY_EDITOR,DEVELOPMENT_BUILD,RELAYLIVELOOP_SYNTHETIC' '/target:exe' "/out:$testExe" $runtimeSources $testSources
    if ($LASTEXITCODE -ne 0) { throw "Synthetic compile failed with exit code $LASTEXITCODE." }
    & $mono $testExe
    if ($LASTEXITCODE -ne 0) { throw "Synthetic tests failed with exit code $LASTEXITCODE." }

    $unityEngine = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine\UnityEngine.CoreModule.dll'
    $unityScreenCapture = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine\UnityEngine.ScreenCaptureModule.dll'
    $netStandard = Join-Path $UnityRoot 'Editor\Data\NetStandard\ref\2.1.0\netstandard.dll'
    $netFxShimRoot = Join-Path $UnityRoot 'Editor\Data\NetStandard\compat\2.1.0\shims\netfx'
    $mscorlib = Join-Path $netFxShimRoot 'mscorlib.dll'
    $system = Join-Path $netFxShimRoot 'System.dll'
    $systemCore = Join-Path $netFxShimRoot 'System.Core.dll'
    $unityCompileDll = Join-Path $outputRoot 'RelayLiveLoop.ObservationUnityCompileCheck.dll'
    & $mono $compiler '/nologo' '/nostdlib+' '/langversion:9.0' '/define:UNITY_EDITOR,DEVELOPMENT_BUILD' '/target:library' "/out:$unityCompileDll" "/r:$mscorlib" "/r:$netStandard" "/r:$system" "/r:$systemCore" "/r:$unityEngine" "/r:$unityScreenCapture" $runtimeSources
    if ($LASTEXITCODE -ne 0) { throw "Unity reference compile failed with exit code $LASTEXITCODE." }
    Write-Output 'UNITY REFERENCE COMPILE PASS: observation, preview/revert, and fresh-frame capture against Unity 2022.3 managed assemblies.'

    $releaseDll = Join-Path $outputRoot 'RelayLiveLoop.ObservationReleaseSurface.dll'
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
