[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$checker = Join-Path $repositoryRoot 'scripts\check-publication.ps1'
$shell = (Get-Process -Id $PID).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("relay-liveloop-publication-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null

function Invoke-TestGit {
    param(
        [string]$Repository,
        [string[]]$GitArguments
    )

    & git -C $Repository @GitArguments 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic git command failed: $($GitArguments -join ' ')"
    }
}

function New-SyntheticRepository {
    param([string]$Name)

    $path = Join-Path $testRoot $Name
    New-Item -ItemType Directory -Path $path | Out-Null
    & git init -b feature/test $path 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to initialize synthetic repository.'
    }
    Invoke-TestGit -Repository $path -GitArguments @('config', 'user.name', 'Synthetic Test')
    Invoke-TestGit -Repository $path -GitArguments @('config', 'user.email', 'synthetic@example.invalid')
    $path
}

function Invoke-PublicationCheckProcess {
    param(
        [string]$Repository,
        [string]$Mode,
        [string]$Patterns
    )

    $output = @(& $shell -NoProfile -File $checker -Repository $Repository -Mode $Mode -ExpectedBranch feature/test -PrivatePatternsFile $Patterns -SkipRemoteCheck 2>&1)
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = @($output | ForEach-Object { $_.ToString() })
    }
}

$patternsPath = Join-Path $testRoot 'synthetic-patterns.json'
@{
    literalPatterns = @('private.invalid', 'Synthetic.Private.Namespace', 'C:\SyntheticProject\Screenshots')
    blockedExtensions = @('.syntheticbinary')
} | ConvertTo-Json | Set-Content -LiteralPath $patternsPath -Encoding utf8

$results = [Collections.Generic.List[object]]::new()
try {
    $positive = New-SyntheticRepository -Name 'positive'
    New-Item -ItemType Directory -Path (Join-Path $positive 'host') | Out-Null
    Set-Content -LiteralPath (Join-Path $positive 'host\service.txt') -Value 'synthetic public content' -Encoding utf8
    Invoke-TestGit -Repository $positive -GitArguments @('add', '--', 'host/service.txt')
    $positiveRun = Invoke-PublicationCheckProcess -Repository $positive -Mode staged -Patterns $patternsPath
    if ($positiveRun.ExitCode -ne 0) { throw "Clean staged content was incorrectly blocked: $($positiveRun.Output -join ' | ')" }
    $results.Add([pscustomobject]@{ Case = 'clean-staged'; Expected = 'pass'; Actual = 'pass' })

    $domain = New-SyntheticRepository -Name 'private-domain'
    New-Item -ItemType Directory -Path (Join-Path $domain 'host') | Out-Null
    Set-Content -LiteralPath (Join-Path $domain 'host\endpoint.txt') -Value 'https://private.invalid/repository' -Encoding utf8
    Invoke-TestGit -Repository $domain -GitArguments @('add', '--', 'host/endpoint.txt')
    $domainRun = Invoke-PublicationCheckProcess -Repository $domain -Mode staged -Patterns $patternsPath
    if ($domainRun.ExitCode -eq 0) { throw 'Synthetic private domain was not blocked.' }
    $results.Add([pscustomobject]@{ Case = 'private-domain-staged'; Expected = 'blocked'; Actual = 'blocked' })

    $code = New-SyntheticRepository -Name 'private-code'
    New-Item -ItemType Directory -Path (Join-Path $code 'host') | Out-Null
    Set-Content -LiteralPath (Join-Path $code 'host\adapter.cs') -Value 'namespace Synthetic.Private.Namespace {}' -Encoding utf8
    Invoke-TestGit -Repository $code -GitArguments @('add', '--', 'host/adapter.cs')
    $codeRun = Invoke-PublicationCheckProcess -Repository $code -Mode staged -Patterns $patternsPath
    if ($codeRun.ExitCode -eq 0) { throw 'Synthetic private code marker was not blocked.' }
    $results.Add([pscustomobject]@{ Case = 'private-code-staged'; Expected = 'blocked'; Actual = 'blocked' })

    $screenshot = New-SyntheticRepository -Name 'screenshot-outgoing'
    New-Item -ItemType Directory -Path (Join-Path $screenshot 'docs') | Out-Null
    Set-Content -LiteralPath (Join-Path $screenshot 'docs\capture.png') -Value 'synthetic screenshot bytes' -Encoding utf8
    Invoke-TestGit -Repository $screenshot -GitArguments @('add', '--', 'docs/capture.png')
    Invoke-TestGit -Repository $screenshot -GitArguments @('commit', '-m', 'Add synthetic capture')
    $screenshotRun = Invoke-PublicationCheckProcess -Repository $screenshot -Mode outgoing -Patterns $patternsPath
    if ($screenshotRun.ExitCode -eq 0) { throw 'Synthetic screenshot was not blocked in outgoing history.' }
    $results.Add([pscustomobject]@{ Case = 'screenshot-outgoing'; Expected = 'blocked'; Actual = 'blocked' })

    $message = New-SyntheticRepository -Name 'message-outgoing'
    New-Item -ItemType Directory -Path (Join-Path $message 'host') | Out-Null
    Set-Content -LiteralPath (Join-Path $message 'host\service.txt') -Value 'synthetic public content' -Encoding utf8
    Invoke-TestGit -Repository $message -GitArguments @('add', '--', 'host/service.txt')
    Invoke-TestGit -Repository $message -GitArguments @('commit', '-m', 'Reference Synthetic.Private.Namespace')
    $messageRun = Invoke-PublicationCheckProcess -Repository $message -Mode outgoing -Patterns $patternsPath
    if ($messageRun.ExitCode -eq 0) { throw 'Synthetic private marker in a commit message was not blocked.' }
    $results.Add([pscustomobject]@{ Case = 'private-message-outgoing'; Expected = 'blocked'; Actual = 'blocked' })

    [pscustomobject]@{
        Status = 'passed'
        Cases = @($results)
    } | ConvertTo-Json -Depth 4
}
finally {
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedTestRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($resolvedTestRoot)).StartsWith('relay-liveloop-publication-test-', [StringComparison]::Ordinal)) {
        throw 'Refusing to remove an unexpected synthetic test path.'
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
