[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [ValidateRange(0, 3600)][int]$WaitSeconds = 60,
    [switch]$StopWhenUpdateStateUnknown,
    [switch]$PreserveEditor
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib\Deployment.Common.ps1')

function Get-OwnedRecordState {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$ExpectedConfigPath,
        [ValidateSet('host', 'editor')][string]$Role
    )

    $path = Get-RelayProcessRecordPath -Config $Config -Role $Role
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $null; identity = $null; stoppable = $false; reason = 'no_process_record' }
    }
    $record = Read-RelayJsonFile -Path $path
    if ($null -eq $record.PSObject.Properties['role'] -or $null -eq $record.PSObject.Properties['ownedByDeployment'] -or $null -eq $record.PSObject.Properties['configPath']) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $null; stoppable = $false; reason = 'record_incomplete' }
    }
    if ($record.role -ne $Role) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $null; stoppable = $false; reason = 'record_role_mismatch' }
    }
    if ($record.ownedByDeployment -ne $true) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $null; stoppable = $false; reason = 'attached_process_not_owned' }
    }
    if (-not [string]::Equals((Get-RelayFullPath -Path ([string]$record.configPath)), (Get-RelayFullPath -Path $ExpectedConfigPath), [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $null; stoppable = $false; reason = 'record_config_mismatch' }
    }
    $identity = Test-RelayProcessIdentity -Record $record
    if (-not $identity.matched) {
        return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $identity; stoppable = $false; reason = $identity.reason }
    }
    return [pscustomobject]@{ role = $Role; path = $path; record = $record; identity = $identity; stoppable = $true; reason = $null }
}

function Remove-OwnedRecordIfUnchanged {
    param([Parameter(Mandatory = $true)]$State)

    if (-not (Test-Path -LiteralPath $State.path -PathType Leaf)) {
        return
    }
    $current = Read-RelayJsonFile -Path $State.path
    if ($null -eq $current.PSObject.Properties['ownershipId'] -or $null -eq $State.record.PSObject.Properties['ownershipId']) {
        throw 'Refusing to remove a process record without a stable ownershipId.'
    }
    if (-not [string]::Equals([string]$current.ownershipId, [string]$State.record.ownershipId, [StringComparison]::Ordinal)) {
        throw 'Process record changed while stopping; refusing to remove the newer record.'
    }
    Remove-Item -LiteralPath $State.path -Force
}

function Stop-ConfirmedOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    if ($State.role -ne 'host') {
        throw 'Refusing forceful termination for a non-Host process. Editor and Player state must be preserved.'
    }

    $processId = [int]$State.record.processId
    if (-not $PSCmdlet.ShouldProcess("owned $($State.role) process $processId", $Reason)) {
        return [ordered]@{ state = 'planned'; processId = $processId; identityMatched = $true }
    }
    Stop-Process -Id $processId -ErrorAction Stop
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        $process.WaitForExit(5000) | Out-Null
    }
    if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        throw "$($State.role) process did not exit after the stop request."
    }
    Remove-OwnedRecordIfUnchanged -State $State
    return [ordered]@{ state = 'stopped'; processId = $processId; identityMatched = $true }
}

function Get-EditorPreservationResult {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][bool]$ExplicitlyRequested
    )

    $processId = $null
    if ($null -ne $State.record -and $null -ne $State.record.PSObject.Properties['processId']) {
        $processId = [int]$State.record.processId
    }
    return [ordered]@{
        state = 'preserved'
        reason = if ($ExplicitlyRequested) { 'preserve_editor_requested' } else { 'editing_state_protection' }
        policy = 'preserve'
        changed = $false
        processId = $processId
        identityMatched = [bool]$State.stoppable
        observation = if ($State.stoppable) { 'owned_process_identified' } else { [string]$State.reason }
    }
}

function Get-LegacyLifecycleGaps {
    return @(
        [ordered]@{
            code = 'HOST_ACTIVE_UPDATE_DETAIL_UNAVAILABLE'
            fact = 'Whether a running update is active and non-interruptible'
            currentInterface = 'This older Host status has no hostLifecycle activeJobs/nonInterruptibleJobs/safeToExit facts.'
        },
        [ordered]@{
            code = 'HOST_GRACEFUL_SHUTDOWN_UNAVAILABLE'
            fact = 'Authenticated graceful Host shutdown'
            currentInterface = 'This older Host exposes no authenticated lifecycle shutdown contract.'
        }
    )
}

try {
    $configFull = Get-RelayExistingPath -Path $ConfigPath -Kind File
    $config = Get-RelayMachineConfig -ConfigPath $configFull
    Assert-RelayMachineConfigPaths -Config $config -ConfigPath $configFull
    $hostState = Get-OwnedRecordState -Config $config -ExpectedConfigPath $configFull -Role host
    $editorState = Get-OwnedRecordState -Config $config -ExpectedConfigPath $configFull -Role editor
    $editorResult = Get-EditorPreservationResult -State $editorState -ExplicitlyRequested ([bool]$PreserveEditor)
    $probe = Get-RelayHostProbe -Config $config -Endpoint status
    $lifecycleView = if ($probe.valid) {
        Get-RelayHostLifecycleView -StatusBody $probe.body
    }
    else {
        [pscustomobject]@{ available = $false; valid = $false; lifecycle = $null; reason = 'host_unreachable_or_unverified' }
    }

    $relevantOwnedHostProcess = [bool]$hostState.stoppable
    if (-not $probe.valid -and -not $relevantOwnedHostProcess) {
        Write-RelayResult -Value ([ordered]@{
            status = 'already_stopped_or_no_owned_process'
            host = [ordered]@{ state = $hostState.reason; identityMatched = $false }
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false }
        })
        exit 0
    }

    if ($probe.valid -and -not $hostState.stoppable) {
        Write-RelayResult -Value ([ordered]@{
            status = 'not_stopped'
            reason = 'reachable_host_has_no_confirmed_owned_identity'
            host = [ordered]@{ state = 'preserved'; identityMatched = $false }
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false }
        })
        exit 3
    }

    if ($lifecycleView.available -and -not $lifecycleView.valid) {
        Write-RelayResult -Value ([ordered]@{
            status = 'not_stopped'
            reason = 'host_lifecycle_contract_invalid'
            lifecycleReason = $lifecycleView.reason
            host = [ordered]@{ state = 'preserved'; identityMatched = [bool]$hostState.stoppable }
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false }
        })
        exit 3
    }

    if ($lifecycleView.valid) {
        $initialLifecycle = $lifecycleView.lifecycle
        $hostProcessId = [int]$hostState.record.processId
        $shutdownTarget = "owned Host process $hostProcessId through POST /lifecycle/shutdown"
        $shutdownAction = 'Request authenticated graceful shutdown with Player preservation and active-job draining'
        if (-not $PSCmdlet.ShouldProcess($shutdownTarget, $shutdownAction)) {
            Write-RelayResult -Value ([ordered]@{
                status = 'planned'
                reason = if ($WhatIfPreference) { 'what_if' } else { 'should_process_declined' }
                preview = [bool]$WhatIfPreference
                host = [ordered]@{
                    state = 'would_request_graceful_shutdown'
                    processId = $hostProcessId
                    identityMatched = $true
                    changed = $false
                }
                editor = $editorResult
                player = [ordered]@{ state = 'preserved'; changed = $false; policy = 'preserve' }
                plannedOperation = [ordered]@{
                    method = 'POST'
                    endpoint = '/lifecycle/shutdown'
                    mode = 'graceful'
                    preservePlayer = $true
                    waitForActiveJobs = $true
                }
                shutdownRequestSent = $false
                processesChanged = $false
                ownershipRecordsChanged = $false
                initialLifecycle = $initialLifecycle
                interfaceFallbackUsed = $false
            })
            exit 0
        }
        $shutdown = Invoke-RelayHostShutdown -Config $config -TimeoutSeconds 10
        if (-not $shutdown.accepted) {
            Write-RelayResult -Value ([ordered]@{
                status = 'not_stopped'
                reason = 'graceful_shutdown_not_accepted'
                host = [ordered]@{ state = 'preserved'; identityMatched = $true }
                editor = $editorResult
                player = [ordered]@{ state = 'preserved'; changed = $false }
                lifecycle = $initialLifecycle
                shutdownResponse = $shutdown.body
            })
            exit 3
        }
        $responseLifecycleView = Get-RelayHostLifecycleView -StatusBody ([pscustomobject]@{ hostLifecycle = $shutdown.body.result.hostLifecycle })
        if (-not $responseLifecycleView.valid -or $shutdown.body.result.hostLifecycle.playerPolicy -ne 'preserve') {
            throw 'Accepted shutdown response does not contain the required preserve-Player lifecycle facts.'
        }
        $acceptedLifecycle = $responseLifecycleView.lifecycle
        $lastLifecycle = $acceptedLifecycle
        $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
        while ($null -ne (Get-Process -Id $hostProcessId -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 250
            $waitProbe = Get-RelayHostProbe -Config $config -Endpoint status -TimeoutSeconds 1
            if ($waitProbe.valid) {
                $waitLifecycle = Get-RelayHostLifecycleView -StatusBody $waitProbe.body
                if ($waitLifecycle.valid) {
                    $lastLifecycle = $waitLifecycle.lifecycle
                }
            }
        }
        $hostStillRunning = $null -ne (Get-Process -Id $hostProcessId -ErrorAction SilentlyContinue)
        if ($hostStillRunning) {
            Write-RelayResult -Value ([ordered]@{
                status = 'draining'
                reason = 'graceful_shutdown_wait_timeout'
                host = [ordered]@{ state = 'draining_not_terminated'; processId = $hostProcessId; identityMatched = $true }
                editor = $editorResult
                player = [ordered]@{ state = 'preserved'; changed = $false; policy = 'preserve' }
                waitSeconds = $WaitSeconds
                initialLifecycle = $initialLifecycle
                acceptedLifecycle = $acceptedLifecycle
                lastLifecycle = $lastLifecycle
                observedActiveJobs = @($acceptedLifecycle.activeJobs)
                observedNonInterruptibleJobs = @($acceptedLifecycle.nonInterruptibleJobs)
            })
            exit 3
        }

        Remove-OwnedRecordIfUnchanged -State $hostState
        $hostResult = [ordered]@{
            state = 'stopped_gracefully'
            processId = $hostProcessId
            identityMatched = $true
            shutdownRequestId = [string]$shutdown.requestId
            waitedForActiveJobs = $true
            hadActiveJobs = @($initialLifecycle.activeJobs).Count -gt 0
            hadNonInterruptibleJobs = @($initialLifecycle.nonInterruptibleJobs).Count -gt 0
        }
        Write-RelayResult -Value ([ordered]@{
            status = 'stopped_gracefully'
            host = $hostResult
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false; policy = 'preserve' }
            initialLifecycle = $initialLifecycle
            acceptedLifecycle = $acceptedLifecycle
            interfaceFallbackUsed = $false
        })
        exit 0
    }

    $legacyGaps = Get-LegacyLifecycleGaps
    if (-not $StopWhenUpdateStateUnknown) {
        Write-RelayResult -Value ([ordered]@{
            status = 'not_stopped'
            reason = 'older_host_lifecycle_unavailable'
            host = [ordered]@{ state = 'owned_process_preserved'; identityMatched = [bool]$hostState.stoppable }
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false }
            interfaceGaps = $legacyGaps
            override = 'Pass -StopWhenUpdateStateUnknown only for an older Host after accepting that update safety cannot be proven.'
        })
        exit 3
    }

    if ($hostState.stoppable) {
        $hostResult = Stop-ConfirmedOwnedProcess -State $hostState -Reason 'Stop older owned Host after explicit unknown-update-state override'
    }
    else {
        $hostResult = [ordered]@{ state = 'preserved'; reason = $hostState.reason; identityMatched = $false }
    }
    if ($hostResult.state -eq 'planned') {
        Write-RelayResult -Value ([ordered]@{
            status = 'planned'
            reason = if ($WhatIfPreference) { 'what_if' } else { 'should_process_declined' }
            preview = [bool]$WhatIfPreference
            host = [ordered]@{
                state = 'would_stop_owned_legacy_host'
                processId = [int]$hostResult.processId
                identityMatched = $true
                changed = $false
            }
            editor = $editorResult
            player = [ordered]@{ state = 'preserved'; changed = $false; policy = 'preserve' }
            plannedOperation = [ordered]@{
                operation = 'stop_owned_legacy_host'
                processId = [int]$hostResult.processId
                lifecycleSafety = 'unknown'
            }
            shutdownRequestSent = $false
            processesChanged = $false
            ownershipRecordsChanged = $false
            interfaceGaps = $legacyGaps
            interfaceFallbackUsed = $true
        })
        exit 0
    }
    Write-RelayResult -Value ([ordered]@{
        status = 'completed_with_older_host_unknown_state_override'
        host = $hostResult
        editor = $editorResult
        player = [ordered]@{ state = 'preserved'; changed = $false }
        interfaceGaps = $legacyGaps
        interfaceFallbackUsed = $true
    })
    exit 0
}
catch {
    Write-RelayResult -Value ([ordered]@{
        status = 'failed'
        player = [ordered]@{ state = 'preserved'; changed = $false }
        error = [ordered]@{ code = 'STOP_FAILED'; message = $_.Exception.Message }
    })
    exit 2
}
