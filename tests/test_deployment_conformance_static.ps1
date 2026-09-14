[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$candidateRoot = Split-Path -Parent $PSScriptRoot
$scriptsRoot = Join-Path $candidateRoot 'scripts'
$assertions = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertions++
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$requiredScripts = @('bootstrap.ps1', 'start.ps1', 'stop.ps1', 'doctor.ps1', 'portable.ps1')
foreach ($name in $requiredScripts) {
    Assert-True (Test-Path -LiteralPath (Join-Path $scriptsRoot $name) -PathType Leaf) "required script exists: $name"
}
Assert-True (Test-Path -LiteralPath (Join-Path $scriptsRoot 'lib\Deployment.Common.ps1') -PathType Leaf) 'shared deployment library exists'

$parseFailures = @()
foreach ($file in @(Get-ChildItem -LiteralPath $scriptsRoot -Filter '*.ps1' -File -Recurse)) {
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    $parseFailures += @($errors | ForEach-Object { "$($file.Name):$($_.Extent.StartLineNumber):$($_.Message)" })
}
Assert-True ($parseFailures.Count -eq 0) ('all PowerShell candidate files parse: ' + ($parseFailures -join '; '))

$allSource = @(Get-ChildItem -LiteralPath $scriptsRoot -Filter '*.ps1' -File -Recurse | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }) -join "`n"
Assert-True ($allSource -notmatch '(?i)C:\\Users\\|E:\\Relay|D:\\Relay|ozdqp|domino') 'public candidates contain no real machine or private project identifiers'
Assert-True ($allSource -notmatch '(?i)--token\s') 'Host command line never receives a raw token option'
Assert-True ($allSource -match '--token-file') 'Host command line uses the existing token-file option'
Assert-True ($allSource -match 'startTimeUtc') 'owned process identity includes start time'
Assert-True ($allSource -match 'HOST_ACTIVE_UPDATE_DETAIL_UNAVAILABLE') 'current Host update-state gap is explicit'
Assert-True ($allSource -match 'NATIVE_HOTFIX_RELOAD_RENDER_EVIDENCE_UNAVAILABLE') 'doctor preserves native evidence gap'
Assert-True ($allSource -match 'migrationValidation\s*=\s*''NOT_RUN''') 'portable export does not claim real migration validation'
Assert-True ($allSource -match '/lifecycle/shutdown') 'normal stop uses the authenticated lifecycle endpoint'
Assert-True ($allSource -match 'preservePlayer\s*=\s*\$true') 'normal stop requests preservePlayer=true'
Assert-True ($allSource -match 'waitForActiveJobs\s*=\s*\$true') 'normal stop requests draining rather than interruption'
Assert-True ($allSource -match 'nonInterruptibleJobs') 'doctor and stop consume exact non-interruptible job facts'

$stopSource = Get-Content -LiteralPath (Join-Path $scriptsRoot 'stop.ps1') -Raw
Assert-True ($stopSource -notmatch '(?i)Get-Process.+Player|Stop-Process.+Player') 'stop script has no Player process termination path'
Assert-True ($stopSource -match 'StopWhenUpdateStateUnknown') 'unknown update state requires an explicit caller override'

$portableSource = Get-Content -LiteralPath (Join-Path $scriptsRoot 'portable.ps1') -Raw
foreach ($blocked in @('Library', '.venv', 'process-state', 'sessions', 'tasks', 'token', 'license', 'ReparsePoint')) {
    Assert-True ($portableSource.IndexOf($blocked, [StringComparison]::OrdinalIgnoreCase) -ge 0) "portable script explicitly handles blocked material: $blocked"
}

Write-Output ([ordered]@{
    status = 'passed'
    test = 'deployment_static_conformance'
    assertions = $assertions
    candidateScripts = $requiredScripts.Count
} | ConvertTo-Json -Compress)
