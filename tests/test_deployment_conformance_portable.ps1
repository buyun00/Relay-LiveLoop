[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$PythonExecutable)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$candidateRoot = Split-Path -Parent $PSScriptRoot
$scriptsRoot = Join-Path $candidateRoot 'scripts'
. (Join-Path $scriptsRoot 'lib\Deployment.Common.ps1')
$powerShellExecutable = (Get-Process -Id $PID).Path
$testOutputRoot = Join-Path ([IO.Path]::GetTempPath()) ('RelayLiveLoop.Tests.' + $PID)
$fixtureRoot = Join-Path $testOutputRoot ('portable fixture-' + [Guid]::NewGuid().ToString('N'))
$assertions = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertions++
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    $script:assertions++
    if (-not [object]::Equals($Expected, $Actual)) {
        throw "Assertion failed: $Message. Expected '$Expected', got '$Actual'."
    }
}

function Invoke-CandidateScript {
    param([string]$ScriptPath, [string[]]$Arguments)
    $raw = @(& $powerShellExecutable -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
    $exitCode = $LASTEXITCODE
    $jsonLine = @($raw | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if ($jsonLine.Count -ne 1) {
        throw "Candidate script emitted no JSON result. Output: $($raw -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{ exitCode = $exitCode; value = ($jsonLine[0] | ConvertFrom-Json); raw = $raw }
}

function New-SyntheticCheckout {
    param([string]$Root, [string]$ToolName = 'ToolCheckout', [string]$GameName = 'GameCheckout')
    $tool = Join-Path $Root $ToolName
    $game = Join-Path $Root $GameName
    $project = Join-Path $game 'Client'
    foreach ($directory in @(
        (Join-Path $tool '.git'), (Join-Path $game '.git'),
        (Join-Path $project 'Assets'), (Join-Path $project 'Packages'), (Join-Path $project 'ProjectSettings')
    )) { [IO.Directory]::CreateDirectory($directory) | Out-Null }
    [IO.File]::WriteAllText((Join-Path $tool 'relay_liveloop.py'), "# synthetic tool marker`r`n")
    [IO.File]::WriteAllText((Join-Path $project 'ProjectSettings\ProjectVersion.txt'), "m_EditorVersion: 2022.3.99f1`r`n")
    return [pscustomobject]@{ tool = $tool; game = $game; project = $project }
}

try {
    [IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
    $sourceRoot = Join-Path $fixtureRoot 'source-machine'
    $source = New-SyntheticCheckout -Root $sourceRoot
    $sourceData = Join-Path $sourceRoot 'RelayData'
    $sourceConfig = Join-Path $sourceRoot 'machine\relay.machine.json'
    $sourceToken = Join-Path $sourceRoot 'machine\relay.token'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $sourceToken)) | Out-Null
    [IO.File]::WriteAllText($sourceToken, "synthetic-portable-source-token`r`n")

    $bootstrap = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'bootstrap.ps1') -Arguments @(
        '-ConfigPath', $sourceConfig,
        '-ToolRepoRoot', $source.tool,
        '-GameRepoRoot', $source.game,
        '-GameProjectRoot', $source.project,
        '-DataRoot', $sourceData,
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $powerShellExecutable,
        '-TokenFile', $sourceToken,
        '-ControlPort', '28080',
        '-RuntimePort', '28081'
    )
    Assert-Equal 0 $bootstrap.exitCode 'source machine bootstrap succeeds'

    $projectConfig = Join-Path $source.project '.relay\project.json'
    $toolLock = Join-Path $source.tool 'requirements.lock'
    $gameLock = Join-Path $source.project 'Packages\packages-lock.json'
    $baseline = Join-Path $sourceData 'baselines\release-a'
    $resources = Join-Path $sourceData 'resources\release-a'
    foreach ($directory in @((Split-Path -Parent $projectConfig), (Split-Path -Parent $gameLock), $baseline, (Join-Path $resources 'textures'))) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    [IO.File]::WriteAllText($projectConfig, "{`"projectId`":`"synthetic-project`"}`r`n")
    [IO.File]::WriteAllText($toolLock, "synthetic-dependency==1.2.3`r`n")
    [IO.File]::WriteAllText($gameLock, "{`"dependencies`":{}}`r`n")
    [IO.File]::WriteAllBytes((Join-Path $baseline 'assembly.bin'), [byte[]](1, 3, 3, 7, 9))
    [IO.File]::WriteAllText((Join-Path $baseline 'baseline.manifest'), "synthetic-baseline-v1`r`n")
    [IO.File]::WriteAllBytes((Join-Path $resources 'textures\neutral-resource.bin'), [byte[]](8, 6, 7, 5, 3, 0, 9))
    [IO.File]::WriteAllText((Join-Path $resources 'resource.manifest'), "synthetic-resource-v1`r`n")

    $archive = Join-Path $sourceData 'portable\synthetic-transfer.zip'
    $export = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Export', '-ConfigPath', $sourceConfig, '-ArchivePath', $archive,
        '-ProjectConfigPath', $projectConfig,
        '-DependencyLockPath', $toolLock,
        '-BaselinePath', $baseline,
        '-ResourcePath', $resources
    )
    Assert-Equal 0 $export.exitCode ('portable export succeeds; output=' + ($export.raw -join ' | '))
    Assert-Equal 'exported' $export.value.status 'portable export reports exported'
    Assert-True ([int]$export.value.fileCount -ge 6) 'portable export manifests every selected file'
    Assert-Equal $false $export.value.exportedLiveState 'portable export excludes live state'
    Assert-Equal $false $export.value.exportedCredentialsOrLicenses 'portable export excludes credentials and licenses'
    Assert-Equal $false $export.value.secondMachineValidated 'export does not claim second-machine validation'

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $zipNames = @($zip.Entries | ForEach-Object { $_.FullName })
        $manifestEntry = $zip.GetEntry('manifest.json')
        $manifestReader = [IO.StreamReader]::new($manifestEntry.Open(), [Text.Encoding]::UTF8)
        try { $manifestText = $manifestReader.ReadToEnd() }
        finally { $manifestReader.Dispose() }
    }
    finally { $zip.Dispose() }
    Assert-True ($zipNames -contains 'manifest.json') 'archive contains a manifest'
    Assert-True (@($zipNames | Where-Object { $_ -match '(?i)token|license|process-state|sessions|tasks|Library|\.venv' }).Count -eq 0) 'archive names contain no blocked state or secret material'
    Assert-True (-not $manifestText.Contains((Get-RelayFullPath -Path $sourceRoot))) 'portable manifest stores root roles and relative paths, not old machine roots'
    Assert-True (-not $manifestText.Contains('synthetic-portable-source-token')) 'portable manifest contains no token value'

    $targetRoot = Join-Path $fixtureRoot 'relocated-machine\checkouts'
    $target = New-SyntheticCheckout -Root $targetRoot
    $targetData = Join-Path $fixtureRoot 'relocated-machine\RelayDataElsewhere'
    $targetConfig = Join-Path $fixtureRoot 'relocated-machine\machine\relay.machine.json'
    $targetToken = Join-Path $fixtureRoot 'relocated-machine\machine\new-machine.token'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $targetToken)) | Out-Null
    [IO.File]::WriteAllText($targetToken, "synthetic-new-machine-token`r`n")

    $import = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Import', '-ConfigPath', $targetConfig, '-ArchivePath', $archive,
        '-DiscoveryRoot', $targetRoot,
        '-NewDataRoot', $targetData,
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $powerShellExecutable,
        '-TokenFile', $targetToken
    )
    Assert-Equal 0 $import.exitCode ('portable import succeeds under changed roots; output=' + ($import.raw -join ' | '))
    Assert-Equal 'imported' $import.value.status 'portable import reports imported'
    Assert-Equal $true $import.value.rootsRediscovered 'portable import reports bounded root discovery'
    Assert-Equal $false $import.value.secondMachineValidated 'changed-root synthetic import is not called a real second-machine validation'

    $targetMachine = Read-RelayJsonFile -Path $targetConfig
    Assert-Equal (Get-RelayFullPath -Path $target.tool) ([string]$targetMachine.toolRepoRoot) 'tool root is rediscovered'
    Assert-Equal (Get-RelayFullPath -Path $target.game) ([string]$targetMachine.gameRepoRoot) 'game repository root is rediscovered'
    Assert-Equal (Get-RelayFullPath -Path $target.project) ([string]$targetMachine.gameProjectRoot) 'Unity project root is rediscovered'
    Assert-Equal (Get-RelayFullPath -Path $targetToken) ([string]$targetMachine.tokenFile) 'new local token reference is used'
    Assert-Equal 'NOT_RUN' ([string]$targetMachine.migration.newMachineValidation) 'real new-machine validation remains NOT_RUN'

    $restoredProjectConfig = Join-Path $target.project '.relay\project.json'
    $restoredToolLock = Join-Path $target.tool 'requirements.lock'
    $restoredBaseline = Join-Path $targetData 'baselines\release-a\assembly.bin'
    $restoredResource = Join-Path $targetData 'resources\release-a\textures\neutral-resource.bin'
    foreach ($pair in @(
        @($projectConfig, $restoredProjectConfig), @($toolLock, $restoredToolLock),
        @((Join-Path $baseline 'assembly.bin'), $restoredBaseline), @((Join-Path $resources 'textures\neutral-resource.bin'), $restoredResource)
    )) {
        Assert-True (Test-Path -LiteralPath $pair[1] -PathType Leaf) "restored file exists: $($pair[1])"
        Assert-Equal (Get-RelayFileSha256 -Path $pair[0]) (Get-RelayFileSha256 -Path $pair[1]) "restored hash matches: $($pair[1])"
    }
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $targetData 'deployment\process-state\host.json'))) 'import does not migrate a live Host process record'

    $collisionRoot = Join-Path $fixtureRoot 'collision-machine\checkouts'
    $collision = New-SyntheticCheckout -Root $collisionRoot
    $collisionLock = Join-Path $collision.tool 'requirements.lock'
    [IO.File]::WriteAllText($collisionLock, "different-existing-lock==9.9.9`r`n")
    $collisionHashBefore = Get-RelayFileSha256 -Path $collisionLock
    $collisionToken = Join-Path $fixtureRoot 'collision-machine\machine\new-machine.token'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $collisionToken)) | Out-Null
    [IO.File]::WriteAllText($collisionToken, "synthetic-collision-machine-token`r`n")
    $collisionConfig = Join-Path $fixtureRoot 'collision-machine\machine\relay.machine.json'
    $collisionImport = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Import', '-ConfigPath', $collisionConfig, '-ArchivePath', $archive,
        '-DiscoveryRoot', $collisionRoot,
        '-NewDataRoot', (Join-Path $fixtureRoot 'collision-machine\data'),
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $powerShellExecutable,
        '-TokenFile', $collisionToken
    )
    Assert-Equal 2 $collisionImport.exitCode 'portable import refuses a different existing dependency lock'
    Assert-Equal $collisionHashBefore (Get-RelayFileSha256 -Path $collisionLock) 'portable import does not overwrite a different existing dependency lock'
    Assert-True (-not (Test-Path -LiteralPath $collisionConfig)) 'collision failure creates no machine configuration'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $collision.project '.relay\project.json'))) 'collision preflight writes no earlier planned repository file'

    $blockedResource = Join-Path $sourceData 'resources\api-token.txt'
    [IO.File]::WriteAllText($blockedResource, "synthetic-secret-that-must-not-export`r`n")
    $blockedArchive = Join-Path $sourceData 'portable\blocked.zip'
    $blockedExport = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Export', '-ConfigPath', $sourceConfig, '-ArchivePath', $blockedArchive,
        '-ProjectConfigPath', $projectConfig,
        '-DependencyLockPath', $toolLock,
        '-BaselinePath', $baseline,
        '-ResourcePath', $blockedResource
    )
    Assert-Equal 2 $blockedExport.exitCode 'portable export rejects token-named material'
    Assert-Equal 'failed' $blockedExport.value.status 'blocked export reports failure'
    Assert-True (-not (Test-Path -LiteralPath $blockedArchive)) 'blocked export creates no archive'

    $tamperStage = Join-Path $fixtureRoot 'tamper-stage'
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $tamperStage)
    $tamperPayload = Get-ChildItem -LiteralPath (Join-Path $tamperStage 'payload') -File -Recurse | Select-Object -First 1
    [IO.File]::AppendAllText($tamperPayload.FullName, 'tampered')
    $tamperedArchive = Join-Path $sourceData 'portable\tampered.zip'
    [IO.Compression.ZipFile]::CreateFromDirectory($tamperStage, $tamperedArchive, [IO.Compression.CompressionLevel]::Optimal, $false)
    $tamperConfig = Join-Path $fixtureRoot 'tamper-import\machine.json'
    $tamperData = Join-Path $fixtureRoot 'tamper-import\data'
    $tamperImport = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Import', '-ConfigPath', $tamperConfig, '-ArchivePath', $tamperedArchive,
        '-DiscoveryRoot', $targetRoot,
        '-NewDataRoot', $tamperData,
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $powerShellExecutable,
        '-TokenFile', $targetToken
    )
    Assert-Equal 2 $tamperImport.exitCode 'portable import rejects a payload hash mismatch'
    Assert-Equal 'failed' $tamperImport.value.status 'tampered import reports failure'
    Assert-True (-not (Test-Path -LiteralPath $tamperConfig)) 'tampered import creates no machine config'

    $maliciousArchive = Join-Path $sourceData 'portable\path-traversal.zip'
    $maliciousStream = [IO.File]::Open($maliciousArchive, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $maliciousZip = [IO.Compression.ZipArchive]::new($maliciousStream, [IO.Compression.ZipArchiveMode]::Create, $false)
    try {
        $maliciousEntry = $maliciousZip.CreateEntry('../escaped.txt')
        $maliciousWriter = [IO.StreamWriter]::new($maliciousEntry.Open())
        try { $maliciousWriter.Write('synthetic path traversal') }
        finally { $maliciousWriter.Dispose() }
    }
    finally {
        $maliciousZip.Dispose()
        $maliciousStream.Dispose()
    }
    $maliciousConfig = Join-Path $fixtureRoot 'malicious-import\machine.json'
    $maliciousData = Join-Path $fixtureRoot 'malicious-import\data'
    $maliciousImport = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'portable.ps1') -Arguments @(
        'Import', '-ConfigPath', $maliciousConfig, '-ArchivePath', $maliciousArchive,
        '-DiscoveryRoot', $targetRoot,
        '-NewDataRoot', $maliciousData,
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $powerShellExecutable,
        '-TokenFile', $targetToken
    )
    Assert-Equal 2 $maliciousImport.exitCode 'portable import rejects archive path traversal'
    Assert-True (-not (Test-Path -LiteralPath $maliciousConfig)) 'path-traversal import creates no machine configuration'
    Assert-True (@(Get-ChildItem -LiteralPath $fixtureRoot -Filter 'escaped.txt' -File -Recurse -ErrorAction SilentlyContinue).Count -eq 0) 'path traversal writes no escaped file'

    Write-RelayResult -Value ([ordered]@{
        status = 'passed'
        test = 'deployment_portable_synthetic'
        assertions = $assertions
        changedRootSimulation = $true
        realSecondMachineValidation = $false
        realUnityLaunched = $false
        realPlayerLaunched = $false
    })
    exit 0
}
finally {
    if (Test-Path -LiteralPath $fixtureRoot) {
        if (-not (Test-RelayPathContained -Parent $testOutputRoot -Candidate $fixtureRoot)) {
            throw 'Refusing recursive cleanup outside the test output root.'
        }
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
    if ((Test-Path -LiteralPath $testOutputRoot -PathType Container) -and @(Get-ChildItem -LiteralPath $testOutputRoot -Force).Count -eq 0) {
        Remove-Item -LiteralPath $testOutputRoot -Force
    }
}
