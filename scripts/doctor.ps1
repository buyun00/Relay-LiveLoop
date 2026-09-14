[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [ValidateRange(1, 30)][int]$TimeoutSeconds = 3
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib\Deployment.Common.ps1')

function Test-DoctorPath {
    param(
        [string]$Path,
        [ValidateSet('File', 'Directory')][string]$Kind
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    $pathType = if ($Kind -eq 'File') { 'Leaf' } else { 'Container' }
    return [bool](Test-Path -LiteralPath (Get-RelayFullPath -Path $Path) -PathType $pathType)
}

function Get-DoctorProcessState {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$ExpectedConfigPath,
        [ValidateSet('host', 'editor')][string]$Role
    )

    $recordPath = Get-RelayProcessRecordPath -Config $Config -Role $Role
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
        return [ordered]@{ state = 'not_recorded'; ownedByDeployment = $null; identityMatched = $false; processId = $null }
    }
    try {
        $record = Read-RelayJsonFile -Path $recordPath
        if ($null -eq $record.PSObject.Properties['configPath'] -or -not [string]::Equals((Get-RelayFullPath -Path ([string]$record.configPath)), (Get-RelayFullPath -Path $ExpectedConfigPath), [StringComparison]::OrdinalIgnoreCase)) {
            return [ordered]@{ state = 'record_config_mismatch'; ownedByDeployment = $null; identityMatched = $false; processId = $null; startTimeRecorded = $null }
        }
        $identity = Test-RelayProcessIdentity -Record $record
        return [ordered]@{
            state = if ($identity.matched) { 'running_identity_confirmed' } else { 'record_stale_or_mismatched' }
            ownedByDeployment = if ($null -ne $record.PSObject.Properties['ownedByDeployment']) { [bool]$record.ownedByDeployment } else { $null }
            identityMatched = [bool]$identity.matched
            identityReason = $identity.reason
            processId = if ($identity.matched) { [int]$record.processId } else { $null }
            startTimeRecorded = if ($null -ne $record.PSObject.Properties['startTimeUtc']) { $true } else { $false }
        }
    }
    catch {
        return [ordered]@{ state = 'record_unreadable'; ownedByDeployment = $null; identityMatched = $false; processId = $null; identityReason = $_.Exception.GetType().Name }
    }
}

try {
    $configFull = Get-RelayExistingPath -Path $ConfigPath -Kind File
    $config = Get-RelayMachineConfig -ConfigPath $configFull

    $installed = [ordered]@{
        toolRepository = (Test-DoctorPath -Path ([string]$config.toolRepoRoot) -Kind Directory) -and (Test-DoctorPath -Path (Join-Path ([string]$config.toolRepoRoot) 'relay_liveloop.py') -Kind File)
        gameRepository = Test-DoctorPath -Path ([string]$config.gameRepoRoot) -Kind Directory
        gameProject = (Test-DoctorPath -Path ([string]$config.gameProjectRoot) -Kind Directory) -and (Test-DoctorPath -Path (Join-Path ([string]$config.gameProjectRoot) 'ProjectSettings\ProjectVersion.txt') -Kind File)
        python = Test-DoctorPath -Path ([string]$config.pythonExecutable) -Kind File
        unity = Test-DoctorPath -Path ([string]$config.unityExecutable) -Kind File
        tokenReference = Test-DoctorPath -Path ([string]$config.tokenFile) -Kind File
        sdkRoots = @($config.sdkRoots | ForEach-Object {
            [ordered]@{ configured = $true; installed = Test-DoctorPath -Path ([string]$_) -Kind Directory }
        })
    }

    $hostProcess = Get-DoctorProcessState -Config $config -ExpectedConfigPath $configFull -Role host
    $editorProcess = Get-DoctorProcessState -Config $config -ExpectedConfigPath $configFull -Role editor
    $statusProbe = Get-RelayHostProbe -Config $config -Endpoint status -TimeoutSeconds $TimeoutSeconds
    $capabilityProbe = if ($statusProbe.valid) {
        Get-RelayHostProbe -Config $config -Endpoint capabilities -TimeoutSeconds $TimeoutSeconds
    }
    else {
        [pscustomobject]@{ reachable = $false; valid = $false; body = $null; error = 'status_not_verified' }
    }

    $capabilities = @()
    if ($capabilityProbe.valid) {
        foreach ($item in @($capabilityProbe.body.capabilities)) {
            $providerId = if ($null -ne $item.PSObject.Properties['providerId']) { $item.providerId } else { $null }
            $verified = $null -ne $item.PSObject.Properties['verified'] -and $item.verified -eq $true
            $available = $null -ne $item.PSObject.Properties['available'] -and $item.available -eq $true
            $capabilities += [ordered]@{
                capability = [string]$item.capability
                installed = $null
                connected = $null
                hostConnected = [bool]$statusProbe.valid
                implemented = -not [string]::IsNullOrWhiteSpace([string]$providerId)
                available = $available
                verified = $verified
                providerId = $providerId
                reason = if ($null -ne $item.PSObject.Properties['reason']) { $item.reason } else { $null }
            }
        }
    }

    $ledger = $null
    $runtime = $null
    $hostLifecycle = $null
    $hostLifecycleView = [pscustomobject]@{ available = $false; valid = $false; lifecycle = $null; reason = 'host_status_unavailable' }
    if ($statusProbe.valid) {
        $ledger = $statusProbe.body.ledger
        $runtime = $statusProbe.body.runtime
        $hostLifecycleView = Get-RelayHostLifecycleView -StatusBody $statusProbe.body
        if ($hostLifecycleView.valid) {
            $hostLifecycle = $hostLifecycleView.lifecycle
        }
    }

    $gaps = @(
        [ordered]@{ code = 'HOST_EDITOR_CONNECTION_EVIDENCE_UNAVAILABLE'; state = 'UNKNOWN'; implication = 'an Editor process record does not prove Host-to-Editor connectivity' },
        [ordered]@{ code = 'HOST_PLAYER_CONNECTION_EVIDENCE_UNAVAILABLE'; state = 'UNKNOWN'; implication = 'Host health does not prove a Player bridge or rendered frame' },
        [ordered]@{ code = 'NATIVE_HOTFIX_RELOAD_RENDER_EVIDENCE_UNAVAILABLE'; state = 'UNVERIFIED'; implication = 'generic capabilities and configured versions are not native Hotfix, Reload, or rendering proof' }
    )
    if (-not $hostLifecycleView.available) {
        $gaps += [ordered]@{ code = 'HOST_ACTIVE_UPDATE_DETAIL_UNAVAILABLE'; state = 'UNKNOWN'; implication = 'this older Host status has no hostLifecycle activeJobs/nonInterruptibleJobs/safeToExit facts' }
        $gaps += [ordered]@{ code = 'HOST_GRACEFUL_SHUTDOWN_UNAVAILABLE'; state = 'UNKNOWN'; implication = 'this older Host has no authenticated lifecycle shutdown contract' }
    }
    elseif (-not $hostLifecycleView.valid) {
        $gaps += [ordered]@{ code = 'HOST_LIFECYCLE_CONTRACT_INVALID'; state = 'UNKNOWN'; implication = [string]$hostLifecycleView.reason }
    }

    $lifecycleCapability = $null
    if ($capabilityProbe.valid -and $null -ne $capabilityProbe.body.PSObject.Properties['hostLifecycle']) {
        $lifecycleCapability = $capabilityProbe.body.hostLifecycle
    }

    Write-RelayResult -Value ([ordered]@{
        status = 'diagnostic_complete'
        readOnly = $true
        configuration = [ordered]@{
            loaded = $true
            schemaVersion = [int]$config.schemaVersion
            unityProjectVersion = if ($null -ne $config.PSObject.Properties['unityProjectVersion']) { [string]$config.unityProjectVersion } else { $null }
            interactiveDesktopVerified = $config.interactiveDesktopVerified -eq $true
            renderAfterRdpDisconnect = [string]$config.renderAfterRdpDisconnect
        }
        installed = $installed
        processes = [ordered]@{ host = $hostProcess; editor = $editorProcess }
        host = [ordered]@{
            reachable = [bool]$statusProbe.reachable
            connected = [bool]$statusProbe.valid
            protocolVerified = [bool]$statusProbe.valid
            statusError = $statusProbe.error
            ledger = $ledger
            runtime = $runtime
            lifecycle = [ordered]@{
                available = [bool]$hostLifecycleView.available
                valid = [bool]$hostLifecycleView.valid
                state = if ($hostLifecycleView.valid) { [string]$hostLifecycle.state } else { 'UNKNOWN' }
                acceptingCommands = if ($hostLifecycleView.valid) { [bool]$hostLifecycle.acceptingCommands } else { $null }
                safeToExit = if ($hostLifecycleView.valid) { [bool]$hostLifecycle.safeToExit } else { $null }
                playerPolicy = if ($hostLifecycleView.valid) { [string]$hostLifecycle.playerPolicy } else { $null }
                activeJobs = if ($hostLifecycleView.valid) { @($hostLifecycle.activeJobs) } else { @() }
                nonInterruptibleJobs = if ($hostLifecycleView.valid) { @($hostLifecycle.nonInterruptibleJobs) } else { @() }
                shutdownContract = $lifecycleCapability
            }
        }
        capabilities = $capabilities
        nativeEvidence = [ordered]@{
            state = 'UNVERIFIED'
            hotfixVerified = $null
            reloadVerified = $null
            renderingVerified = $null
            playerInputVerified = $null
        }
        gaps = $gaps
    })
    exit 0
}
catch {
    Write-RelayResult -Value ([ordered]@{
        status = 'failed'
        readOnly = $true
        error = [ordered]@{ code = 'DOCTOR_FAILED'; message = $_.Exception.Message }
    })
    exit 2
}
