param([Parameter(Mandatory=$true)][string]$UnityRoot)
$ErrorActionPreference = 'Stop'
$testRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$repositoryRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $testRoot))
$editorRoot = Join-Path $repositoryRoot 'unity-package\Editor\Core'
$mono = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\bin\mono.exe'
$compiler = Join-Path $UnityRoot 'Editor\Data\MonoBleedingEdge\lib\mono\msbuild\Current\bin\Roslyn\csc.exe'
$temporaryBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$outputRoot = Join-Path $temporaryBase ('relay-liveloop-source-save-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $outputRoot | Out-Null
try {
    $testExe = Join-Path $outputRoot 'SourceSaveSynthetic.exe'
    $testSources = @((Join-Path $editorRoot 'EditorJobContracts.cs'),
        (Join-Path $editorRoot 'NeutralEditorProviderContracts.cs'), (Join-Path $editorRoot 'DurableSourceEdit.cs'),
        (Join-Path $repositoryRoot 'tests\unity-synthetic\native-unknown\EditorJsonUtilityStub.cs'),
        (Join-Path $testRoot 'SyntheticSourceSaveTests.cs'))
    & $mono $compiler '/nologo' '/langversion:9.0' '/define:UNITY_EDITOR,RELAYLIVELOOP_EDITOR_STORE_SYNTHETIC' '/target:exe' "/out:$testExe" '/r:System.Runtime.Serialization.dll' $testSources
    if ($LASTEXITCODE -ne 0) { throw 'Source-save synthetic C# compilation failed.' }
    & $mono $testExe (Join-Path $outputRoot 'receipts')
    if ($LASTEXITCODE -ne 0) { throw 'Source-save synthetic assertions failed.' }
    $runtimeSources = @(Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'unity-package\Runtime\Core') -File -Filter '*.cs' | Select-Object -ExpandProperty FullName)
    $editorSources = @(Get-ChildItem -LiteralPath $editorRoot -File -Filter '*.cs' | Select-Object -ExpandProperty FullName)
    $managed = Join-Path $UnityRoot 'Editor\Data\Managed\UnityEngine'
    $shim = Join-Path $UnityRoot 'Editor\Data\NetStandard\compat\2.1.0\shims\netfx'
    $references = @((Join-Path $shim 'mscorlib.dll'), (Join-Path $shim 'System.dll'), (Join-Path $shim 'System.Core.dll'),
        (Join-Path $UnityRoot 'Editor\Data\NetStandard\ref\2.1.0\netstandard.dll'),
        (Join-Path $managed 'UnityEngine.CoreModule.dll'), (Join-Path $managed 'UnityEngine.JSONSerializeModule.dll'),
        (Join-Path $managed 'UnityEngine.ScreenCaptureModule.dll'), (Join-Path $managed 'UnityEditor.CoreModule.dll'))
    $compileArguments = @('/nologo','/noconfig','/nostdlib+','/langversion:9.0','/define:UNITY_EDITOR,DEVELOPMENT_BUILD','/target:library',
        ('/out:' + (Join-Path $outputRoot 'RelayLiveLoop.ReferenceCheck.dll'))) + @($references | ForEach-Object { '/r:' + $_ }) + $runtimeSources + $editorSources
    & $mono $compiler $compileArguments
    if ($LASTEXITCODE -ne 0) { throw 'Unity 2022.3 managed-reference compile failed.' }
    Write-Output 'UNITY MANAGED REFERENCE PASS: all Runtime/Core and Editor/Core compiled; Editor was not launched.'
}
finally {
    $resolved = [IO.Path]::GetFullPath($outputRoot)
    if (!$resolved.StartsWith($temporaryBase + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $resolved -notmatch 'relay-liveloop-source-save-[0-9a-f]{32}$') { throw 'Synthetic cleanup path escaped the expected temporary root.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
