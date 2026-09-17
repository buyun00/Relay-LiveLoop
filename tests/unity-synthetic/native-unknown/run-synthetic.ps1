param(
    [string]$UnityRoot
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($UnityRoot)) {
    $unityEditorBase = Join-Path $env:ProgramFiles 'Unity\Hub\Editor'
    $unityRootCandidate = Get-ChildItem -LiteralPath $unityEditorBase -Directory -Filter '2022.3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($null -ne $unityRootCandidate) { $UnityRoot = $unityRootCandidate.FullName }
}
if ([string]::IsNullOrWhiteSpace($UnityRoot)) { throw 'Unity 2022.3 root was not supplied or discovered.' }

$mono = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\bin\mono.exe'
$compiler = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\lib\mono\msbuild\Current\bin\Roslyn\csc.exe'
if (!(Test-Path -LiteralPath $mono -PathType Leaf) -or !(Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'Unity-bundled Mono or Roslyn compiler was not found.'
}

$testRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $testRoot))))
$packageRoot = Join-Path $repositoryRoot 'unity-package'
if (!(Test-Path -LiteralPath (Join-Path $packageRoot 'package.json') -PathType Leaf)) {
    throw 'Relay LiveLoop package root was not found.'
}

$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar)
$temporaryRoot = Join-Path $temporaryBase ('relay-liveloop-native-unknown-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
$fullTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
$temporaryPrefix = $temporaryBase + [IO.Path]::DirectorySeparatorChar
if (!$fullTemporaryRoot.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Synthetic output directory escaped the system temporary directory.'
}

$priorWirePath = $env:RELAY_LIVELOOP_CSHARP_UNKNOWN_WIRE
$locationPushed = $false
$crossBoundaryPassed = $false
try {
    $runtimeSources = @(Get-ChildItem -LiteralPath (Join-Path $packageRoot 'Runtime\Core') -File -Filter '*.cs' |
        Sort-Object Name | Select-Object -ExpandProperty FullName)
    $unityStubs = Join-Path $repositoryRoot 'tests\unity-synthetic\observation-preview\UnityStubs.cs'
    $testSource = Join-Path $testRoot 'SyntheticUnknownTests.cs'
    $testExe = Join-Path $temporaryRoot 'RelayLiveLoop.NativeUnknownSyntheticTests.exe'
    $wirePath = Join-Path $temporaryRoot 'actual-adapter-error.json'
    $arguments = @(
        '/nologo',
        '/langversion:9.0',
        '/define:DEVELOPMENT_BUILD,RELAYLIVELOOP_SYNTHETIC',
        '/target:exe',
        "/out:$testExe",
        '/r:System.Runtime.Serialization.dll'
    ) + $runtimeSources + @($unityStubs, $testSource)
    & $mono $compiler $arguments
    if ($LASTEXITCODE -ne 0) { throw "Synthetic C# compile failed with exit code $LASTEXITCODE." }
    & $mono $testExe $wirePath
    if ($LASTEXITCODE -ne 0) { throw "Synthetic C# adapter test failed with exit code $LASTEXITCODE." }

    $editorCore = Join-Path $repositoryRoot 'unity-package\Editor\Core'
    $editorSources = @(
        (Join-Path $editorCore 'AtomicEditorJobStore.cs'),
        (Join-Path $editorCore 'EditorJobContracts.cs'),
        (Join-Path $editorCore 'EditorJobProviderRegistry.cs'),
        (Join-Path $editorCore 'RelayLiveLoopEditorWorker.cs')
    )
    $editorJsonStub = Join-Path $testRoot 'EditorJsonUtilityStub.cs'
    $editorTestSource = Join-Path $testRoot 'SyntheticEditorJobTombstoneTests.cs'
    $editorTestExe = Join-Path $temporaryRoot 'RelayLiveLoop.EditorJobTombstoneSyntheticTests.exe'
    $editorArguments = @(
        '/nologo',
        '/langversion:9.0',
        '/define:UNITY_EDITOR,RELAYLIVELOOP_EDITOR_STORE_SYNTHETIC',
        '/target:exe',
        "/out:$editorTestExe",
        '/r:System.Runtime.Serialization.dll'
    ) + $editorSources + @($editorJsonStub, $editorTestSource)
    & $mono $compiler $editorArguments
    if ($LASTEXITCODE -ne 0) { throw "Synthetic Editor mailbox compile failed with exit code $LASTEXITCODE." }
    & $mono $editorTestExe (Join-Path $temporaryRoot 'editor-mailbox')
    if ($LASTEXITCODE -ne 0) { throw "Synthetic Editor mailbox replay test failed with exit code $LASTEXITCODE." }

    $env:RELAY_LIVELOOP_CSHARP_UNKNOWN_WIRE = $wirePath
    Push-Location $repositoryRoot
    $locationPushed = $true
    & py -3 -m unittest tests.test_native_runtime_wiring.NativeRuntimeWiringTests.test_actual_csharp_unknown_error_wire_retires_host_session_before_reconcile
    if ($LASTEXITCODE -ne 0) { throw "Host parser/coordinator regression failed with exit code $LASTEXITCODE." }
    $crossBoundaryPassed = $true
    Write-Output 'CROSS-BOUNDARY PASS: actual C# adapter wire fixture retired the Host session before reconciliation.'
}
finally {
    if ($locationPushed) { Pop-Location }
    $env:RELAY_LIVELOOP_CSHARP_UNKNOWN_WIRE = $priorWirePath
    $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $temporaryItem = Get-Item -LiteralPath $resolvedTemporaryRoot -ErrorAction SilentlyContinue
    if (
        $crossBoundaryPassed -and
        $resolvedTemporaryRoot.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $resolvedTemporaryRoot -match '^.*relay-liveloop-native-unknown-[0-9a-f]{32}$' -and
        $null -ne $temporaryItem -and
        $temporaryItem.PSIsContainer -and
        (($temporaryItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
    ) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    } elseif ($null -ne $temporaryItem) {
        Write-Warning "Synthetic output retained for failure inspection: $resolvedTemporaryRoot"
    }
}
