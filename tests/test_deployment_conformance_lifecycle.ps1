[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ToolRepoRoot,
    [Parameter(Mandatory = $true)][string]$PythonExecutable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$candidateRoot = Split-Path -Parent $PSScriptRoot
$scriptsRoot = Join-Path $candidateRoot 'scripts'
. (Join-Path $scriptsRoot 'lib\Deployment.Common.ps1')
$powerShellExecutable = (Get-Process -Id $PID).Path
$testOutputRoot = Join-Path ([IO.Path]::GetTempPath()) ('RelayLiveLoop.Tests.' + $PID)
$fixtureRoot = Join-Path $testOutputRoot ('lifecycle fixture-' + [Guid]::NewGuid().ToString('N'))
$assertions = 0
$ownedProcessIds = [Collections.Generic.List[int]]::new()
$priorBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'

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

function Get-EphemeralPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Stop-TestOwnedProcess {
    param([int]$ProcessId, [string]$StartTimeUtc)
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    $actualStart = $process.StartTime.ToUniversalTime().ToString('o')
    if (-not [string]::Equals($actualStart, $StartTimeUtc, [StringComparison]::Ordinal)) {
        throw 'Refusing test cleanup because process start time changed.'
    }
    Stop-Process -Id $ProcessId -Force
}

try {
    [IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
    $gameRepo = Join-Path $fixtureRoot 'GameRepo'
    $gameProject = Join-Path $gameRepo 'SyntheticProject'
    foreach ($directory in @(
        (Join-Path $gameRepo '.git'),
        (Join-Path $gameProject 'Assets'),
        (Join-Path $gameProject 'Packages'),
        (Join-Path $gameProject 'ProjectSettings')
    )) { [IO.Directory]::CreateDirectory($directory) | Out-Null }
    [IO.File]::WriteAllText((Join-Path $gameProject 'ProjectSettings\ProjectVersion.txt'), "m_EditorVersion: 2022.3.99f1`r`n")

    $helperExecutable = Join-Path $fixtureRoot 'SyntheticEditorHelper.exe'
    $helperSourcePath = Join-Path $PSScriptRoot 'fixtures\SyntheticProcessHelper.cs'
    $compiler = @(
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($null -eq $compiler) { throw 'The Windows .NET Framework C# compiler required for the synthetic helper was not found.' }
    & $compiler /nologo /target:winexe ("/out:$helperExecutable") $helperSourcePath
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic process helper compilation failed.' }
    Assert-True (Test-Path -LiteralPath $helperExecutable -PathType Leaf) 'synthetic non-GUI Editor helper compiled'

    $configPath = Join-Path $fixtureRoot 'machine\relay.machine.json'
    $dataRoot = Join-Path $fixtureRoot 'data'
    $tokenFile = Join-Path $fixtureRoot 'machine\existing.token'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $tokenFile)) | Out-Null
    $tokenText = 'synthetic-lifecycle-token-do-not-log'
    [IO.File]::WriteAllText($tokenFile, $tokenText + [Environment]::NewLine)
    $tokenHashBefore = Get-RelayFileSha256 -Path $tokenFile
    $sdkRoot = Join-Path $fixtureRoot 'SyntheticSdk'
    [IO.Directory]::CreateDirectory($sdkRoot) | Out-Null
    $controlPort = Get-EphemeralPort
    while ($controlPort -in @(18760, 18761)) { $controlPort = Get-EphemeralPort }
    $runtimePort = Get-EphemeralPort
    while ($runtimePort -in @(18760, 18761, $controlPort)) { $runtimePort = Get-EphemeralPort }

    $bootstrapArgs = @(
        '-ConfigPath', $configPath,
        '-ToolRepoRoot', $ToolRepoRoot,
        '-GameRepoRoot', $gameRepo,
        '-GameProjectRoot', $gameProject,
        '-DataRoot', $dataRoot,
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $helperExecutable,
        '-SdkRoot', $sdkRoot,
        '-TokenFile', $tokenFile,
        '-ControlPort', [string]$controlPort,
        '-RuntimePort', [string]$runtimePort
    )
    $bootstrap = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'bootstrap.ps1') -Arguments $bootstrapArgs
    Assert-Equal 0 $bootstrap.exitCode 'bootstrap succeeds'
    Assert-Equal 'configured' $bootstrap.value.status 'bootstrap reports configured'
    Assert-Equal $false $bootstrap.value.tokenCreated 'existing token is preserved'
    Assert-Equal 1 ([int]$bootstrap.value.sdkRootsValidated) 'configured installed SDK root is validated'
    Assert-Equal $false $bootstrap.value.commercialPackagesInstalledOrUpgraded 'bootstrap does not install or upgrade commercial packages'
    Assert-Equal $tokenHashBefore (Get-RelayFileSha256 -Path $tokenFile) 'existing token hash is unchanged'
    Assert-True (-not (($bootstrap.raw -join "`n").Contains($tokenText))) 'token value is never printed'

    $configHashBefore = Get-RelayFileSha256 -Path $configPath
    $bootstrapAgain = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'bootstrap.ps1') -Arguments $bootstrapArgs
    Assert-Equal 0 $bootstrapAgain.exitCode 'second bootstrap succeeds idempotently'
    Assert-Equal 'already_configured' $bootstrapAgain.value.status 'second bootstrap reports existing config'
    Assert-Equal $configHashBefore (Get-RelayFileSha256 -Path $configPath) 'existing config is not overwritten'

    $generatedConfig = Join-Path $fixtureRoot 'generated-machine\relay.machine.json'
    $generatedControlPort = Get-EphemeralPort
    while ($generatedControlPort -in @(18760, 18761)) { $generatedControlPort = Get-EphemeralPort }
    $generatedRuntimePort = Get-EphemeralPort
    while ($generatedRuntimePort -in @(18760, 18761, $generatedControlPort)) { $generatedRuntimePort = Get-EphemeralPort }
    $generatedBootstrap = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'bootstrap.ps1') -Arguments @(
        '-ConfigPath', $generatedConfig,
        '-ToolRepoRoot', $ToolRepoRoot,
        '-GameRepoRoot', $gameRepo,
        '-GameProjectRoot', $gameProject,
        '-DataRoot', (Join-Path $fixtureRoot 'generated-data'),
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $helperExecutable,
        '-ControlPort', [string]$generatedControlPort,
        '-RuntimePort', [string]$generatedRuntimePort
    )
    Assert-Equal 0 $generatedBootstrap.exitCode 'bootstrap can create a new private token reference'
    Assert-Equal $true $generatedBootstrap.value.tokenCreated 'bootstrap reports new token creation without printing the token'
    $generatedMachine = Read-RelayJsonFile -Path $generatedConfig
    Assert-True (Test-Path -LiteralPath ([string]$generatedMachine.tokenFile) -PathType Leaf) 'generated token file exists outside repositories'
    $generatedTokenText = (Get-Content -LiteralPath ([string]$generatedMachine.tokenFile) -Raw).Trim()
    Assert-True (-not (($generatedBootstrap.raw -join "`n").Contains($generatedTokenText))) 'new token value is not printed'

    $insideRepoConfig = Join-Path $gameRepo 'unsafe-machine.json'
    $insideRepoBootstrap = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'bootstrap.ps1') -Arguments @(
        '-ConfigPath', $insideRepoConfig,
        '-ToolRepoRoot', $ToolRepoRoot,
        '-GameRepoRoot', $gameRepo,
        '-GameProjectRoot', $gameProject,
        '-DataRoot', (Join-Path $fixtureRoot 'unsafe-data'),
        '-PythonExecutable', $PythonExecutable,
        '-UnityExecutable', $helperExecutable
    )
    Assert-Equal 2 $insideRepoBootstrap.exitCode 'bootstrap rejects machine configuration inside a repository'
    Assert-True (-not (Test-Path -LiteralPath $insideRepoConfig)) 'rejected in-repository configuration is not created'

    $start = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'start.ps1') -Arguments @('-ConfigPath', $configPath, '-HostStartupTimeoutSeconds', '20')
    Assert-Equal 0 $start.exitCode 'start succeeds'
    Assert-Equal 'started' $start.value.host.state 'Host starts from exact CLI surface'
    Assert-Equal 'started' $start.value.editor.state 'synthetic Editor helper starts'
    Assert-Equal $true $start.value.player.preserved 'Player is preserved during start'
    $ownedProcessIds.Add([int]$start.value.host.processId)
    $ownedProcessIds.Add([int]$start.value.editor.processId)
    $hostStartTime = [string]$start.value.host.startTimeUtc
    $editorStartTime = (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o')

    $argumentRecord = Join-Path $gameProject 'synthetic-editor-arguments.txt'
    $argumentDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $argumentRecord) -and [DateTime]::UtcNow -lt $argumentDeadline) { Start-Sleep -Milliseconds 100 }
    Assert-True (Test-Path -LiteralPath $argumentRecord -PathType Leaf) 'Editor helper recorded its arguments'
    $editorArguments = @(Get-Content -LiteralPath $argumentRecord)
    Assert-True ($editorArguments -contains '-projectPath') 'Editor receives -projectPath'
    Assert-True ($editorArguments -contains (Get-RelayFullPath -Path $gameProject)) 'Editor receives the exact configured project root'

    $hostRecord = Read-RelayJsonFile -Path (Join-Path $dataRoot 'deployment\process-state\host.json')
    Assert-Equal ([int]$start.value.host.processId) ([int]$hostRecord.processId) 'Host record stores PID'
    Assert-Equal $hostStartTime ([string]$hostRecord.startTimeUtc) 'Host record stores immutable process start time'
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$hostRecord.ownershipId)) 'Host record stores ownership marker'
    $hostIdentityBeforeSecondStart = Test-RelayProcessIdentity -Record $hostRecord
    Assert-True $hostIdentityBeforeSecondStart.matched ('Host identity record matches before idempotent start; reason=' + $hostIdentityBeforeSecondStart.reason + '; recorded=' + [string]$hostRecord.startTimeUtc + '; actual=' + [string]$hostIdentityBeforeSecondStart.snapshot.startTimeUtc)

    $startAgain = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'start.ps1') -Arguments @('-ConfigPath', $configPath)
    Assert-Equal 0 $startAgain.exitCode ('second start succeeds idempotently; output=' + ($startAgain.raw -join ' | '))
    Assert-Equal 'already_started' $startAgain.value.host.state 'second start reuses owned Host'
    Assert-Equal ([int]$start.value.host.processId) ([int]$startAgain.value.host.processId) 'second start keeps Host PID'
    Assert-Equal 'already_started' $startAgain.value.editor.state 'second start reuses owned Editor'
    Assert-Equal ([int]$start.value.editor.processId) ([int]$startAgain.value.editor.processId) 'second start keeps Editor PID'

    $directLifecycleProbe = Get-RelayHostProbe -Config (Get-RelayMachineConfig -ConfigPath $configPath) -Endpoint status
    Assert-Equal $false $directLifecycleProbe.body.hostLifecycle.safeToExit ('raw running Host safeToExit is reported exactly rather than inferred from job counts; lifecycle=' + (ConvertTo-RelayJson -Value $directLifecycleProbe.body.hostLifecycle))
    $hostRecordPath = Join-Path $dataRoot 'deployment\process-state\host.json'
    $editorRecordPath = Join-Path $dataRoot 'deployment\process-state\editor.json'
    $doctorHashesBefore = @{
        config = Get-RelayFileSha256 -Path $configPath
        host = Get-RelayFileSha256 -Path $hostRecordPath
        editor = Get-RelayFileSha256 -Path $editorRecordPath
    }
    $doctor = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'doctor.ps1') -Arguments @('-ConfigPath', $configPath)
    Assert-Equal 0 $doctor.exitCode 'doctor completes'
    Assert-Equal $true $doctor.value.readOnly 'doctor declares read-only behavior'
    Assert-Equal $true $doctor.value.host.connected 'doctor proves authenticated Host connection'
    Assert-True (@($doctor.value.capabilities).Count -gt 0) 'doctor reports known Host capabilities'
    Assert-True (@($doctor.value.capabilities | Where-Object { $_.verified -eq $true }).Count -eq 0) 'doctor does not invent verified capabilities'
    Assert-Equal 'UNVERIFIED' $doctor.value.nativeEvidence.state 'doctor does not equate HTTP health with native Hotfix proof'
    Assert-True ($null -eq $doctor.value.nativeEvidence.hotfixVerified) 'unverified native Hotfix remains unknown rather than false proof'
    Assert-Equal $true $doctor.value.host.lifecycle.available 'doctor detects the authenticated Host lifecycle contract'
    Assert-Equal $true $doctor.value.host.lifecycle.valid 'doctor validates the Host lifecycle shape'
    Assert-Equal 'running' $doctor.value.host.lifecycle.state 'doctor reports running lifecycle state'
    Assert-Equal $false $doctor.value.host.lifecycle.safeToExit ('doctor preserves the exact running Host safeToExit fact; output=' + ($doctor.raw -join ' | '))
    Assert-Equal 'preserve' $doctor.value.host.lifecycle.playerPolicy 'doctor reports preserve-Player lifecycle policy'
    Assert-True (@($doctor.value.gaps | Where-Object { $_.code -eq 'HOST_ACTIVE_UPDATE_DETAIL_UNAVAILABLE' }).Count -eq 0) 'current Host does not use the older active-update gap fallback'
    Assert-Equal $true $doctor.value.installed.python 'doctor distinguishes installed Python'
    Assert-True ($null -eq $doctor.value.capabilities[0].connected) 'doctor leaves provider connection unknown when the Host does not expose it'
    Assert-Equal $false $doctor.value.capabilities[0].implemented 'doctor distinguishes unimplemented provider capability'
    Assert-Equal $false $doctor.value.capabilities[0].verified 'doctor distinguishes unverified provider capability'
    Assert-Equal $doctorHashesBefore.config (Get-RelayFileSha256 -Path $configPath) 'doctor does not modify machine configuration'
    Assert-Equal $doctorHashesBefore.host (Get-RelayFileSha256 -Path $hostRecordPath) 'doctor does not modify Host identity state'
    Assert-Equal $doctorHashesBefore.editor (Get-RelayFileSha256 -Path $editorRecordPath) 'doctor does not modify Editor identity state'

    $playerProcess = Start-RelayHiddenProcess -Executable $helperExecutable -Arguments @('--synthetic-player') -WorkingDirectory $fixtureRoot
    $ownedProcessIds.Add($playerProcess.Id)
    $playerStartTime = $playerProcess.StartTime.ToUniversalTime().ToString('o')

    # Regression policy: stop.ps1 advertises SupportsShouldProcess, so preview must not send the
    # current Host shutdown request or mutate process/ownership state. These assertions fail on
    # batch 002, where -WhatIf still calls POST /lifecycle/shutdown and stops the Host.
    $whatIfHostRecordHash = Get-RelayFileSha256 -Path $hostRecordPath
    $whatIfEditorRecordHash = Get-RelayFileSha256 -Path $editorRecordPath
    $whatIfStatusBefore = Get-RelayHostProbe -Config (Get-RelayMachineConfig -ConfigPath $configPath) -Endpoint status
    $whatIfLedgerBefore = ConvertTo-RelayJson -Value $whatIfStatusBefore.body.ledger
    $stopWhatIf = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '15', '-WhatIf')
    Assert-Equal 0 $stopWhatIf.exitCode ('current Host WhatIf succeeds as a non-mutating preview; output=' + ($stopWhatIf.raw -join ' | '))
    Assert-Equal 'planned' $stopWhatIf.value.status 'current Host WhatIf reports a planned operation rather than stopped or draining'
    Assert-Equal 'what_if' $stopWhatIf.value.reason 'current Host preview identifies the WhatIf decision'
    Assert-Equal $true $stopWhatIf.value.preview 'current Host response identifies preview mode'
    Assert-Equal 'would_request_graceful_shutdown' $stopWhatIf.value.host.state 'current Host response truthfully describes the planned shutdown request'
    Assert-Equal $false $stopWhatIf.value.host.changed 'current Host preview reports no process change'
    Assert-Equal $false $stopWhatIf.value.shutdownRequestSent 'current Host preview reports no shutdown request sent'
    Assert-Equal $false $stopWhatIf.value.processesChanged 'current Host preview reports no process change globally'
    Assert-Equal $false $stopWhatIf.value.ownershipRecordsChanged 'current Host preview reports no ownership-record change'
    Assert-Equal 'preserved' $stopWhatIf.value.editor.state 'current Host preview preserves Editor'
    Assert-Equal $false $stopWhatIf.value.editor.changed 'current Host preview reports no Editor change'
    Assert-Equal 'preserved' $stopWhatIf.value.player.state 'current Host preview preserves Player'
    Assert-Equal $false $stopWhatIf.value.player.changed 'current Host preview reports no Player change'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.host.processId) -ErrorAction SilentlyContinue)) 'Host remains running after current Host WhatIf'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor remains running after current Host WhatIf'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player remains running after current Host WhatIf'
    Assert-Equal $whatIfHostRecordHash (Get-RelayFileSha256 -Path $hostRecordPath) 'current Host WhatIf leaves Host ownership record unchanged'
    Assert-Equal $whatIfEditorRecordHash (Get-RelayFileSha256 -Path $editorRecordPath) 'current Host WhatIf leaves Editor ownership record unchanged'
    $whatIfStatusAfter = Get-RelayHostProbe -Config (Get-RelayMachineConfig -ConfigPath $configPath) -Endpoint status
    Assert-Equal $true $whatIfStatusAfter.valid 'Host still accepts an authenticated status request after WhatIf'
    Assert-Equal 'running' $whatIfStatusAfter.body.hostLifecycle.state 'Host lifecycle remains running after WhatIf'
    Assert-Equal $true $whatIfStatusAfter.body.hostLifecycle.acceptingCommands 'Host remains accepting after WhatIf'
    Assert-Equal $false $whatIfStatusAfter.body.hostLifecycle.shutdownRequested 'WhatIf creates no Host shutdown decision'
    Assert-True ($null -eq $whatIfStatusAfter.body.hostLifecycle.shutdownRequestId) 'WhatIf creates no shutdown request ID'
    Assert-Equal $whatIfLedgerBefore (ConvertTo-RelayJson -Value $whatIfStatusAfter.body.ledger) 'WhatIf creates no Host ledger result'

    $stopDefault = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '15')
    Assert-Equal 0 $stopDefault.exitCode ('normal graceful stop succeeds; output=' + ($stopDefault.raw -join ' | '))
    Assert-Equal 'stopped_gracefully' $stopDefault.value.status 'normal stop uses authenticated graceful lifecycle'
    Assert-Equal 'stopped_gracefully' $stopDefault.value.host.state 'Host exits through lifecycle endpoint rather than Stop-Process'
    Assert-Equal 'preserved' $stopDefault.value.editor.state 'default normal stop preserves the Editor after graceful Host exit'
    Assert-Equal 'editing_state_protection' $stopDefault.value.editor.reason 'default normal stop explains the Editor preservation policy'
    Assert-Equal 'preserve' $stopDefault.value.editor.policy 'default normal stop reports explicit Editor preserve policy'
    Assert-Equal $false $stopDefault.value.editor.changed 'default normal stop reports that it did not change the Editor process'
    Assert-Equal $true $stopDefault.value.editor.identityMatched 'default normal stop can identify the owned Editor without terminating it'
    Assert-Equal $false $stopDefault.value.interfaceFallbackUsed 'normal stop does not use older-Host fallback'
    Assert-Equal 'preserve' $stopDefault.value.player.policy 'shutdown response preserves Player policy'
    Assert-True ($null -eq (Get-Process -Id ([int]$start.value.host.processId) -ErrorAction SilentlyContinue)) 'Host process exited gracefully'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'owned Editor survives the default normal stop'
    Assert-True (Test-Path -LiteralPath $editorRecordPath -PathType Leaf) 'preserved Editor ownership record remains available without claiming process closure'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'synthetic Player remains after normal graceful stop'

    $startDrain = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'start.ps1') -Arguments @('-ConfigPath', $configPath, '-HostOnly')
    Assert-Equal 0 $startDrain.exitCode ('Host restart for drain test succeeds; output=' + ($startDrain.raw -join ' | '))
    Assert-Equal 'started' $startDrain.value.host.state 'fresh owned Host starts for drain test'
    $ownedProcessIds.Add([int]$startDrain.value.host.processId)
    $drainJobId = 'job_synthetic_non_interruptible_deployment'
    $jobHelper = Join-Path $PSScriptRoot 'fixtures\manage_synthetic_job.py'
    & $PythonExecutable $jobHelper seed --tool-root $ToolRepoRoot --database (Join-Path $dataRoot 'host\relay-liveloop.sqlite3') --job-id $drainJobId
    Assert-Equal 0 $LASTEXITCODE 'synthetic non-interruptible job is seeded in the isolated ledger'

    $doctorActive = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'doctor.ps1') -Arguments @('-ConfigPath', $configPath)
    Assert-Equal 0 $doctorActive.exitCode 'doctor reads active lifecycle facts'
    Assert-Equal $false $doctorActive.value.host.lifecycle.safeToExit 'active job makes safeToExit false'
    Assert-Equal 1 @($doctorActive.value.host.lifecycle.activeJobs).Count 'doctor reports exact active job count'
    Assert-Equal 1 @($doctorActive.value.host.lifecycle.nonInterruptibleJobs).Count 'doctor reports exact non-interruptible job count'
    Assert-Equal 'runtime_apply' ([string]$doctorActive.value.host.lifecycle.nonInterruptibleJobs[0].stage) 'doctor preserves non-interruptible stage'
    Assert-Equal $false $doctorActive.value.host.lifecycle.nonInterruptibleJobs[0].interruptible 'doctor preserves interruptibility fact'

    $stopDraining = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '1')
    Assert-Equal 3 $stopDraining.exitCode 'stop returns a draining result when active work outlives the wait'
    Assert-Equal 'draining' $stopDraining.value.status 'stop reports draining instead of killing Host'
    Assert-Equal 'draining_not_terminated' $stopDraining.value.host.state 'non-interruptible update Host is not terminated'
    Assert-Equal 1 @($stopDraining.value.observedNonInterruptibleJobs).Count 'stop reports non-interruptible job evidence'
    Assert-Equal $drainJobId ([string]$stopDraining.value.observedNonInterruptibleJobs[0].jobId) 'stop reports exact draining job ID'
    Assert-Equal 'preserved' $stopDraining.value.editor.state 'Editor remains preserved while Host drains'
    Assert-Equal 'editing_state_protection' $stopDraining.value.editor.reason 'draining response retains explicit Editor preservation reason'
    Assert-Equal 'preserve' $stopDraining.value.player.policy 'draining stop preserves Player policy'
    Assert-True ($null -ne (Get-Process -Id ([int]$startDrain.value.host.processId) -ErrorAction SilentlyContinue)) 'Host remains live while non-interruptible job is active'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor remains live while Host drains'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player remains live while Host drains'

    & $PythonExecutable $jobHelper complete --tool-root $ToolRepoRoot --database (Join-Path $dataRoot 'host\relay-liveloop.sqlite3') --job-id $drainJobId
    Assert-Equal 0 $LASTEXITCODE 'synthetic job reaches terminal durable state'
    $drainExitDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while ($null -ne (Get-Process -Id ([int]$startDrain.value.host.processId) -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $drainExitDeadline) { Start-Sleep -Milliseconds 100 }
    Assert-True ($null -eq (Get-Process -Id ([int]$startDrain.value.host.processId) -ErrorAction SilentlyContinue)) 'draining Host exits only after the job becomes terminal'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player survives completed drain and Host exit'

    $legacyHelper = Join-Path $PSScriptRoot 'fixtures\synthetic_legacy_host.py'
    $legacyArguments = @($legacyHelper, '--host', '127.0.0.1', '--port', [string]$controlPort, '--token-file', $tokenFile)
    $legacyProcess = Start-RelayHiddenProcess -Executable $PythonExecutable -Arguments $legacyArguments -WorkingDirectory $fixtureRoot
    $ownedProcessIds.Add($legacyProcess.Id)
    $legacyRecord = New-RelayProcessRecord -Process $legacyProcess -Role host -Owned $true -ConfigPath $configPath -ArgumentAnchors @($legacyHelper, '--port', [string]$controlPort)
    Write-RelayJsonFile -Path (Join-Path $dataRoot 'deployment\process-state\host.json') -Value $legacyRecord -AllowReplace | Out-Null
    $legacyDeadline = [DateTime]::UtcNow.AddSeconds(5)
    $legacyProbe = $null
    while ([DateTime]::UtcNow -lt $legacyDeadline) {
        $legacyProbe = Get-RelayHostProbe -Config (Get-RelayMachineConfig -ConfigPath $configPath) -Endpoint status -TimeoutSeconds 1
        if ($legacyProbe.valid) { break }
        Start-Sleep -Milliseconds 100
    }
    Assert-Equal $true $legacyProbe.valid 'synthetic older Host exposes authenticated status'

    $legacyDoctor = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'doctor.ps1') -Arguments @('-ConfigPath', $configPath)
    Assert-Equal 0 $legacyDoctor.exitCode 'doctor supports an older Host status shape'
    Assert-Equal $false $legacyDoctor.value.host.lifecycle.available 'doctor does not invent lifecycle availability for an older Host'
    Assert-True (@($legacyDoctor.value.gaps | Where-Object { $_.code -eq 'HOST_ACTIVE_UPDATE_DETAIL_UNAVAILABLE' }).Count -eq 1) 'doctor reports older-Host update detail gap'
    Assert-True (@($legacyDoctor.value.gaps | Where-Object { $_.code -eq 'HOST_GRACEFUL_SHUTDOWN_UNAVAILABLE' }).Count -eq 1) 'doctor reports older-Host graceful shutdown gap'

    $legacyHostRecordPath = Join-Path $dataRoot 'deployment\process-state\host.json'
    $legacyWhatIfHostRecordHash = Get-RelayFileSha256 -Path $legacyHostRecordPath
    $legacyWhatIfEditorRecordHash = Get-RelayFileSha256 -Path $editorRecordPath
    $legacyStopWhatIf = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '0', '-StopWhenUpdateStateUnknown', '-WhatIf')
    Assert-Equal 0 $legacyStopWhatIf.exitCode ('legacy Host WhatIf succeeds as a non-mutating preview; output=' + ($legacyStopWhatIf.raw -join ' | '))
    Assert-Equal 'planned' $legacyStopWhatIf.value.status 'legacy Host WhatIf reports planned instead of completed'
    Assert-Equal 'what_if' $legacyStopWhatIf.value.reason 'legacy Host preview identifies the WhatIf decision'
    Assert-Equal $true $legacyStopWhatIf.value.preview 'legacy Host response identifies preview mode'
    Assert-Equal 'would_stop_owned_legacy_host' $legacyStopWhatIf.value.host.state 'legacy Host response truthfully describes the planned Host-only stop'
    Assert-Equal $false $legacyStopWhatIf.value.host.changed 'legacy Host preview reports no Host change'
    Assert-Equal $false $legacyStopWhatIf.value.processesChanged 'legacy Host preview reports no process changes'
    Assert-Equal $false $legacyStopWhatIf.value.ownershipRecordsChanged 'legacy Host preview reports no ownership-record changes'
    Assert-Equal 'preserved' $legacyStopWhatIf.value.editor.state 'legacy Host preview preserves Editor'
    Assert-Equal 'preserved' $legacyStopWhatIf.value.player.state 'legacy Host preview preserves Player'
    Assert-True ($null -ne (Get-Process -Id $legacyProcess.Id -ErrorAction SilentlyContinue)) 'legacy Host remains running after WhatIf'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor remains running after legacy Host WhatIf'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player remains running after legacy Host WhatIf'
    Assert-Equal $legacyWhatIfHostRecordHash (Get-RelayFileSha256 -Path $legacyHostRecordPath) 'legacy Host WhatIf leaves Host ownership record unchanged'
    Assert-Equal $legacyWhatIfEditorRecordHash (Get-RelayFileSha256 -Path $editorRecordPath) 'legacy Host WhatIf leaves Editor ownership record unchanged'

    $legacyStopDefault = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '0')
    Assert-Equal 3 $legacyStopDefault.exitCode 'normal stop refuses to kill an older Host with unknown update state'
    Assert-Equal 'older_host_lifecycle_unavailable' $legacyStopDefault.value.reason 'older Host fallback is explicit'
    Assert-True ($null -ne (Get-Process -Id $legacyProcess.Id -ErrorAction SilentlyContinue)) 'older Host stays running after normal fallback refusal'
    Assert-Equal 'preserved' $legacyStopDefault.value.editor.state 'older-Host fallback refusal preserves Editor state'
    Assert-Equal 'editing_state_protection' $legacyStopDefault.value.editor.reason 'older-Host fallback refusal explains Editor preservation'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor stays running after older-Host fallback refusal'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player stays running after older Host fallback refusal'

    $legacyStopOverride = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-WaitSeconds', '0', '-StopWhenUpdateStateUnknown')
    Assert-Equal 0 $legacyStopOverride.exitCode 'explicit older-Host override stops only the identified owned Host'
    Assert-Equal 'completed_with_older_host_unknown_state_override' $legacyStopOverride.value.status 'older Host override remains visibly separate from graceful stop'
    Assert-Equal $true $legacyStopOverride.value.interfaceFallbackUsed 'older Host override reports fallback use'
    Assert-True ($null -eq (Get-Process -Id $legacyProcess.Id -ErrorAction SilentlyContinue)) 'identified older Host exits after explicit fallback override'
    Assert-Equal 'preserved' $legacyStopOverride.value.editor.state 'older-Host override never terminates Editor'
    Assert-Equal 'editing_state_protection' $legacyStopOverride.value.editor.reason 'older-Host override reports the Editor preservation reason'
    Assert-Equal $false $legacyStopOverride.value.editor.changed 'older-Host override reports no Editor process change'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor survives the older-Host override'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'Player survives older Host override'

    $forgedRecordPath = Join-Path $dataRoot 'deployment\process-state\host.json'
    $forgedRecord = [ordered]@{
        recordVersion = 1; role = 'host'; processId = $playerProcess.Id
        startTimeUtc = [DateTime]::UtcNow.AddDays(-1).ToString('o')
        executablePath = $playerProcess.Path; ownedByDeployment = $true
        ownershipId = 'owned_synthetic_forgery'; configPath = $configPath
        projectPath = $null; argumentAnchors = @(); recordedAtUtc = [DateTime]::UtcNow.ToString('o')
    }
    Write-RelayJsonFile -Path $forgedRecordPath -Value $forgedRecord -AllowReplace | Out-Null
    $stopForged = Invoke-CandidateScript -ScriptPath (Join-Path $scriptsRoot 'stop.ps1') -Arguments @('-ConfigPath', $configPath, '-StopWhenUpdateStateUnknown')
    Assert-Equal 0 $stopForged.exitCode 'stop handles stale forged identity without process termination'
    Assert-Equal $false $stopForged.value.host.identityMatched 'forged PID-only record is rejected'
    Assert-True ($null -ne (Get-Process -Id ([int]$start.value.editor.processId) -ErrorAction SilentlyContinue)) 'Editor survives a forged Host record and explicit older-Host fallback'
    Assert-True ($null -ne (Get-Process -Id $playerProcess.Id -ErrorAction SilentlyContinue)) 'unrelated process survives forged PID record'

    Stop-TestOwnedProcess -ProcessId ([int]$start.value.editor.processId) -StartTimeUtc $editorStartTime
    $ownedProcessIds.Remove([int]$start.value.editor.processId) | Out-Null
    Stop-TestOwnedProcess -ProcessId $playerProcess.Id -StartTimeUtc $playerStartTime
    $ownedProcessIds.Remove($playerProcess.Id) | Out-Null
    Write-RelayResult -Value ([ordered]@{
        status = 'passed'
        test = 'deployment_lifecycle_synthetic'
        assertions = $assertions
        controlPort = $controlPort
        runtimePort = $runtimePort
        realUnityLaunched = $false
        realPlayerLaunched = $false
    })
    exit 0
}
finally {
    $env:PYTHONDONTWRITEBYTECODE = $priorBytecodeSetting
    foreach ($processId in @($ownedProcessIds)) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
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
