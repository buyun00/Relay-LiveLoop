[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [ValidateRange(1, 120)][int]$HostStartupTimeoutSeconds = 15,
    [switch]$HostOnly
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib\Deployment.Common.ps1')

function Get-MatchingRelayEditors {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$ProjectPath
    )

    $leaf = [IO.Path]::GetFileName($ExecutablePath).Replace("'", "''")
    $editorMatches = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name = '$leaf'" -ErrorAction Stop)) {
        if ([string]::IsNullOrWhiteSpace($candidate.ExecutablePath) -or [string]::IsNullOrWhiteSpace($candidate.CommandLine)) {
            continue
        }
        $candidatePath = Get-RelayFullPath -Path $candidate.ExecutablePath
        if (-not [string]::Equals($candidatePath, $ExecutablePath, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $hasProjectFlag = $candidate.CommandLine -match '(?i)(?:^|\s)-projectPath(?:\s|=)'
        $hasExactProjectText = $candidate.CommandLine.IndexOf($ProjectPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        if ($hasProjectFlag -and $hasExactProjectText) {
            $editorMatches += Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction Stop
        }
    }
    return @($editorMatches)
}

$hostResult = $null
$editorResult = $null
try {
    $configFull = Get-RelayExistingPath -Path $ConfigPath -Kind File
    $config = Get-RelayMachineConfig -ConfigPath $configFull
    Assert-RelayMachineConfigPaths -Config $config -ConfigPath $configFull -RequireUnity:(-not $HostOnly)
    $toolRoot = Get-RelayExistingPath -Path ([string]$config.toolRepoRoot) -Kind Directory
    $relayCli = Get-RelayExistingPath -Path (Join-Path $toolRoot 'relay_liveloop.py') -Kind File
    $python = Get-RelayExistingPath -Path ([string]$config.pythonExecutable) -Kind File
    $stateRoot = Get-RelayStateRoot -Config $config
    [IO.Directory]::CreateDirectory($stateRoot) | Out-Null

    $preflightEditors = @()
    $preflightOwnedEditorRecord = $null
    if (-not $HostOnly) {
        $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
        if ($currentIdentity.User.Value -eq 'S-1-5-18' -or -not [Environment]::UserInteractive) {
            throw 'Editor start requires the logged-in user interactive desktop and will not run as LocalSystem.'
        }
        $preflightEditors = @(Get-MatchingRelayEditors -ExecutablePath (Get-RelayFullPath -Path ([string]$config.unityExecutable)) -ProjectPath (Get-RelayFullPath -Path ([string]$config.gameProjectRoot)))
        if ($preflightEditors.Count -gt 1) {
            throw 'More than one Editor process matches the configured project; refusing to choose or replace one.'
        }
        if ($preflightEditors.Count -eq 1) {
            $existingEditorRecordPath = Get-RelayProcessRecordPath -Config $config -Role editor
            if (Test-Path -LiteralPath $existingEditorRecordPath -PathType Leaf) {
                $existingEditorRecord = Read-RelayJsonFile -Path $existingEditorRecordPath
                $existingEditorIdentity = Test-RelayProcessIdentity -Record $existingEditorRecord
                $editorConfigMatches = $null -ne $existingEditorRecord.PSObject.Properties['configPath'] -and [string]::Equals((Get-RelayFullPath -Path ([string]$existingEditorRecord.configPath)), $configFull, [StringComparison]::OrdinalIgnoreCase)
                if ($existingEditorIdentity.matched -and $editorConfigMatches -and $existingEditorRecord.ownedByDeployment -eq $true -and [int]$existingEditorRecord.processId -eq $preflightEditors[0].Id) {
                    $preflightOwnedEditorRecord = $existingEditorRecord
                }
            }
        }
    }

    $probe = Get-RelayHostProbe -Config $config -Endpoint status
    if ($probe.reachable) {
        if (-not $probe.valid) {
            throw 'The configured port answered but did not identify the Relay LiveLoop protocol.'
        }
        $lifecycleView = Get-RelayHostLifecycleView -StatusBody $probe.body
        if ($lifecycleView.available -and -not $lifecycleView.valid) {
            throw "Host lifecycle status is present but invalid: $($lifecycleView.reason)"
        }
        if ($lifecycleView.valid -and ($lifecycleView.lifecycle.state -eq 'draining' -or $lifecycleView.lifecycle.acceptingCommands -ne $true)) {
            throw 'The configured Host is draining and cannot be treated as ready or replaced.'
        }
        $hostRecordPath = Get-RelayProcessRecordPath -Config $config -Role host
        $ownedHostRecord = $null
        if (Test-Path -LiteralPath $hostRecordPath -PathType Leaf) {
            $candidateHostRecord = Read-RelayJsonFile -Path $hostRecordPath
            $candidateHostIdentity = Test-RelayProcessIdentity -Record $candidateHostRecord
            $hostConfigMatches = $null -ne $candidateHostRecord.PSObject.Properties['configPath'] -and [string]::Equals((Get-RelayFullPath -Path ([string]$candidateHostRecord.configPath)), $configFull, [StringComparison]::OrdinalIgnoreCase)
            if ($candidateHostIdentity.matched -and $hostConfigMatches -and $candidateHostRecord.ownedByDeployment -eq $true) {
                $ownedHostRecord = $candidateHostRecord
            }
        }
        $hostResult = if ($null -ne $ownedHostRecord) {
            [ordered]@{
                state = 'already_started'
                processId = [int]$ownedHostRecord.processId
                startTimeUtc = ConvertTo-RelayUtcTimestamp -Value $ownedHostRecord.startTimeUtc
                ownedByDeployment = $true
                protocolVerified = $true
            }
        }
        else {
            [ordered]@{
                state = 'attached'
                processId = $null
                ownedByDeployment = $false
                protocolVerified = $true
            }
        }
    }
    else {
        $hostRecordPath = Get-RelayProcessRecordPath -Config $config -Role host
        if (Test-Path -LiteralPath $hostRecordPath -PathType Leaf) {
            $priorRecord = Read-RelayJsonFile -Path $hostRecordPath
            $priorIdentity = Test-RelayProcessIdentity -Record $priorRecord
            if ($priorIdentity.matched) {
                throw 'The recorded Host process is still running but the authenticated Host endpoint is unavailable; refusing to start a duplicate.'
            }
        }

        $databasePath = Get-RelayFullPath -Path ([string]$config.lifecycle.databasePath)
        [IO.Directory]::CreateDirectory((Split-Path -Parent $databasePath)) | Out-Null
        $hostArguments = @($relayCli, 'serve', '--database', $databasePath)
        foreach ($artifactRootValue in @($config.lifecycle.artifactRoots)) {
            $artifactRoot = Get-RelayFullPath -Path ([string]$artifactRootValue)
            [IO.Directory]::CreateDirectory($artifactRoot) | Out-Null
            $hostArguments += @('--artifact-root', $artifactRoot)
        }
        $hostArguments += @(
            '--host', [string]$config.controlAddress,
            '--port', ([string][int]$config.controlPort),
            '--token-file', (Get-RelayFullPath -Path ([string]$config.tokenFile)
            )
        )

        if (-not $PSCmdlet.ShouldProcess('configured Relay LiveLoop Host', 'Start owned non-GUI Host process')) {
            $hostResult = [ordered]@{ state = 'planned'; processId = $null; ownedByDeployment = $false; protocolVerified = $false }
        }
        else {
            $priorBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
            try {
                $env:PYTHONDONTWRITEBYTECODE = '1'
                $hostProcess = Start-RelayHiddenProcess -Executable $python -Arguments $hostArguments -WorkingDirectory $toolRoot
            }
            finally {
                $env:PYTHONDONTWRITEBYTECODE = $priorBytecodeSetting
            }
            $hostRecord = New-RelayProcessRecord -Process $hostProcess -Role host -Owned $true -ConfigPath $configFull -ArgumentAnchors @($relayCli, 'serve', $databasePath)
            Write-RelayJsonFile -Path $hostRecordPath -Value $hostRecord -AllowReplace | Out-Null

            $deadline = [DateTime]::UtcNow.AddSeconds($HostStartupTimeoutSeconds)
            $readyProbe = $null
            while ([DateTime]::UtcNow -lt $deadline) {
                $readyProbe = Get-RelayHostProbe -Config $config -Endpoint status -TimeoutSeconds 1
                if ($readyProbe.reachable -and $readyProbe.valid) {
                    break
                }
                if ($null -eq (Get-Process -Id $hostProcess.Id -ErrorAction SilentlyContinue)) {
                    break
                }
                Start-Sleep -Milliseconds 250
            }
            if ($null -eq $readyProbe -or -not $readyProbe.reachable -or -not $readyProbe.valid) {
                $identity = Test-RelayProcessIdentity -Record $hostRecord
                if ($identity.matched) {
                    Stop-Process -Id ([int]$hostRecord.processId) -Force -ErrorAction SilentlyContinue
                }
                if (Test-Path -LiteralPath $hostRecordPath -PathType Leaf) {
                    Remove-Item -LiteralPath $hostRecordPath -Force
                }
                throw 'Owned Host did not expose the authenticated status endpoint before the startup timeout.'
            }
            $readyLifecycle = Get-RelayHostLifecycleView -StatusBody $readyProbe.body
            if ($readyLifecycle.available -and (-not $readyLifecycle.valid -or $readyLifecycle.lifecycle.state -ne 'running' -or $readyLifecycle.lifecycle.acceptingCommands -ne $true)) {
                throw 'Owned Host status did not report a valid accepting lifecycle state.'
            }
            $hostResult = [ordered]@{
                state = 'started'
                processId = [int]$hostRecord.processId
                startTimeUtc = [string]$hostRecord.startTimeUtc
                ownedByDeployment = $true
                protocolVerified = $true
            }
        }
    }

    if ($HostOnly) {
        $editorResult = [ordered]@{ state = 'skipped'; reason = 'HostOnly'; ownedByDeployment = $false }
    }
    elseif ($preflightEditors.Count -eq 1 -and $null -ne $preflightOwnedEditorRecord) {
        $editorResult = [ordered]@{
            state = 'already_started'
            processId = [int]$preflightOwnedEditorRecord.processId
            startTimeUtc = ConvertTo-RelayUtcTimestamp -Value $preflightOwnedEditorRecord.startTimeUtc
            ownedByDeployment = $true
        }
    }
    elseif ($preflightEditors.Count -eq 1) {
        $editorProcess = $preflightEditors[0]
        $editorRecord = New-RelayProcessRecord -Process $editorProcess -Role editor -Owned $false -ConfigPath $configFull -ProjectPath ([string]$config.gameProjectRoot) -ArgumentAnchors @('-projectPath', [string]$config.gameProjectRoot)
        Write-RelayJsonFile -Path (Get-RelayProcessRecordPath -Config $config -Role editor) -Value $editorRecord -AllowReplace | Out-Null
        $editorResult = [ordered]@{
            state = 'attached'
            processId = [int]$editorRecord.processId
            startTimeUtc = [string]$editorRecord.startTimeUtc
            ownedByDeployment = $false
        }
    }
    elseif (-not $PSCmdlet.ShouldProcess('configured current Editor project', 'Start Editor in the current interactive user session')) {
        $editorResult = [ordered]@{ state = 'planned'; processId = $null; ownedByDeployment = $false }
    }
    else {
        $unity = Get-RelayExistingPath -Path ([string]$config.unityExecutable) -Kind File
        $projectRoot = Get-RelayExistingPath -Path ([string]$config.gameProjectRoot) -Kind Directory
        $editorProcess = Start-RelayHiddenProcess -Executable $unity -Arguments @('-projectPath', $projectRoot) -WorkingDirectory $projectRoot
        Start-Sleep -Milliseconds 250
        $editorRecord = New-RelayProcessRecord -Process $editorProcess -Role editor -Owned $true -ConfigPath $configFull -ProjectPath $projectRoot -ArgumentAnchors @('-projectPath', $projectRoot)
        Write-RelayJsonFile -Path (Get-RelayProcessRecordPath -Config $config -Role editor) -Value $editorRecord -AllowReplace | Out-Null
        $editorResult = [ordered]@{
            state = 'started'
            processId = [int]$editorRecord.processId
            startTimeUtc = [string]$editorRecord.startTimeUtc
            ownedByDeployment = $true
        }
    }

    Write-RelayResult -Value ([ordered]@{
        status = 'ready'
        host = $hostResult
        editor = $editorResult
        player = [ordered]@{ changed = $false; preserved = $true }
        nativeRuntimeVerified = $false
    })
    exit 0
}
catch {
    Write-RelayResult -Value ([ordered]@{
        status = 'failed'
        host = $hostResult
        editor = $editorResult
        player = [ordered]@{ changed = $false; preserved = $true }
        error = [ordered]@{ code = 'START_FAILED'; message = $_.Exception.Message }
    })
    exit 2
}
