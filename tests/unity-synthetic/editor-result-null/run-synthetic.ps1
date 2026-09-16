param(
    [string]$UnityRoot,
    [string]$RepositoryRoot
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

$testRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$testRoot = [IO.Path]::GetFullPath($testRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $testRoot))
}
$packageRoot = Join-Path $RepositoryRoot 'unity-package'
$atomicStore = Join-Path $packageRoot 'Editor\Core\AtomicEditorJobStore.cs'
$contracts = Join-Path $packageRoot 'Editor\Core\EditorJobContracts.cs'
foreach ($path in @($atomicStore, $contracts)) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required source was not found: $path" }
}

$outputRoot = [IO.Path]::GetFullPath((Join-Path $testRoot ('.out-' + [Guid]::NewGuid().ToString('N').Substring(0, 6))))
$testRootPrefix = $testRoot + [IO.Path]::DirectorySeparatorChar
if ($outputRoot -eq $testRoot -or !$outputRoot.StartsWith($testRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Focused output directory escaped the test root.'
}
$createdOutputRoot = $false

function Test-SafeFocusedOutputRoot {
    param(
        [string]$Candidate,
        [string]$ExpectedTestRoot
    )

    try {
        $resolvedRoot = [IO.Path]::GetFullPath($ExpectedTestRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
        $resolvedCandidate = [IO.Path]::GetFullPath($Candidate)
        $prefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
        if ($resolvedCandidate -eq $resolvedRoot -or !$resolvedCandidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }

        $item = Get-Item -LiteralPath $resolvedCandidate -Force -ErrorAction Stop
        if (!$item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            return $false
        }

        $resolvedPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $resolvedCandidate -ErrorAction Stop).Path)
        return [String]::Equals($resolvedPath, $resolvedCandidate, [StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

try {
    if (Test-Path -LiteralPath $outputRoot) {
        throw "Focused output directory collision: $outputRoot"
    }
    New-Item -ItemType Directory -Path $outputRoot -ErrorAction Stop | Out-Null
    $createdOutputRoot = $true

    $testExe = Join-Path $outputRoot 'EditorResultNullSerializationTests.exe'
    $sources = @(
        $contracts,
        $atomicStore,
        (Join-Path $testRoot 'UnityJsonUtilityFailureStub.cs'),
        (Join-Path $testRoot 'EditorResultNullSerializationTests.cs')
    )
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:UNITY_EDITOR,RELAYLIVELOOP_SYNTHETIC' '/target:exe' "/out:$testExe" '/r:System.Web.Extensions.dll' $sources
    if ($LASTEXITCODE -ne 0) { throw "Focused C# compile failed with exit code $LASTEXITCODE." }
    & $mono $testExe
    if ($LASTEXITCODE -ne 0) { throw "Focused C# test failed with exit code $LASTEXITCODE." }
}
finally {
    if ($createdOutputRoot) {
        if (!(Test-SafeFocusedOutputRoot -Candidate $outputRoot -ExpectedTestRoot $testRoot)) {
            throw "Refusing to remove an output directory outside the expected test root: $outputRoot"
        }
        Remove-Item -LiteralPath $outputRoot -Recurse -Force -ErrorAction Stop
    }
}

