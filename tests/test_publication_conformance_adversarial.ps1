[CmdletBinding()]
param(
    [string]$CheckerPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\check-publication.ps1'),
    [string]$ScratchParent = [IO.Path]::GetTempPath(),
    [string]$ResultPath,
    [switch]$KeepFixtures
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$checkerItem = Get-Item -LiteralPath $CheckerPath -Force -ErrorAction Stop
if ($checkerItem.PSIsContainer) {
    throw 'CheckerPath must name a file.'
}
$CheckerPath = [IO.Path]::GetFullPath($checkerItem.FullName)

$scratchItem = Get-Item -LiteralPath $ScratchParent -Force -ErrorAction Stop
if (-not $scratchItem.PSIsContainer) {
    throw 'ScratchParent must name an existing directory.'
}
$ScratchParent = [IO.Path]::GetFullPath($scratchItem.FullName).TrimEnd('\', '/')
$runRoot = Join-Path $ScratchParent ('relay-liveloop-publication-conformance-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runRoot | Out-Null

$isolatedGlobalConfig = Join-Path $runRoot 'isolated-global.gitconfig'
Set-Content -LiteralPath $isolatedGlobalConfig -Value '' -Encoding ascii
$patternsPath = Join-Path $runRoot 'synthetic-patterns.json'
$privateLiteral = 'restricted.synthetic.invalid'
$privateCodeMarker = 'Synthetic.Restricted.Marker'
@{
    literalPatterns = @($privateLiteral, $privateCodeMarker)
    blockedExtensions = @('.syntheticblocked')
} | ConvertTo-Json | Set-Content -LiteralPath $patternsPath -Encoding utf8

$shell = (Get-Process -Id $PID).Path
$results = [Collections.Generic.List[object]]::new()
$script:checkSequence = 0

function Invoke-IsolatedNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [hashtable]$ExtraEnvironment = @{}
    )

    $environment = @{
        GIT_CONFIG_GLOBAL = $isolatedGlobalConfig
        GIT_CONFIG_NOSYSTEM = '1'
        GIT_TERMINAL_PROMPT = '0'
    }
    foreach ($entry in $ExtraEnvironment.GetEnumerator()) {
        $environment[[string]$entry.Key] = [string]$entry.Value
    }

    $prior = @{}
    foreach ($key in $environment.Keys) {
        $prior[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $environment[$key], 'Process')
    }

    try {
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        foreach ($key in $environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $prior[$key], 'Process')
        }
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output | ForEach-Object { $_.ToString() })
    }
}

function Invoke-TestGit {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repository,
        [Parameter(Mandatory = $true)]
        [string[]]$GitArguments,
        [hashtable]$ExtraEnvironment = @{}
    )

    $arguments = @('-C', $Repository) + $GitArguments
    $run = Invoke-IsolatedNative -FilePath 'git' -Arguments $arguments -ExtraEnvironment $ExtraEnvironment
    if ($run.ExitCode -ne 0) {
        throw "Synthetic git command failed: git $($GitArguments -join ' ') :: $($run.Output -join ' | ')"
    }
    @($run.Output)
}

function New-SyntheticRepository {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$Branch = 'feature/test'
    )

    $path = Join-Path $runRoot $Name
    New-Item -ItemType Directory -Path $path | Out-Null
    $null = Invoke-TestGit -Repository $path -GitArguments @('init', '--template=', '-b', $Branch, '.')
    $null = Invoke-TestGit -Repository $path -GitArguments @('config', 'user.name', 'Synthetic Conformance Test')
    $null = Invoke-TestGit -Repository $path -GitArguments @('config', 'user.email', 'synthetic-conformance@example.invalid')
    $null = Invoke-TestGit -Repository $path -GitArguments @('config', 'commit.gpgsign', 'false')
    $null = Invoke-TestGit -Repository $path -GitArguments @('config', 'core.autocrlf', 'false')
    $path
}

function Add-BaselineCommit {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repository
    )

    Set-Content -LiteralPath (Join-Path $Repository 'README.md') -Value 'Synthetic public baseline.' -Encoding utf8
    $null = Invoke-TestGit -Repository $Repository -GitArguments @('add', '--', 'README.md')
    $null = Invoke-TestGit -Repository $Repository -GitArguments @('commit', '-m', 'Add synthetic baseline')
}

function New-LocalBareRemote {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $path = Join-Path $runRoot $Name
    $null = Invoke-TestGit -Repository $runRoot -GitArguments @('init', '--template=', '--bare', '-b', 'feature/test', $path)
    [IO.Path]::GetFullPath($path)
}

function Connect-And-PushBaseline {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repository,
        [Parameter(Mandatory = $true)]
        [string]$RemotePath
    )

    $null = Invoke-TestGit -Repository $Repository -GitArguments @('remote', 'add', 'origin', $RemotePath)
    $null = Invoke-TestGit -Repository $Repository -GitArguments @('push', 'origin', 'HEAD:refs/heads/feature/test')
}

function Invoke-PublicationChecker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repository,
        [ValidateSet('staged', 'outgoing', 'all')]
        [string]$Mode = 'staged',
        [string]$ExpectedBranch = 'feature/test',
        [string]$ExpectedRemoteUrl,
        [switch]$SkipRemoteCheck,
        [hashtable]$ExtraEnvironment = @{}
    )

    $script:checkSequence++
    $reportPath = Join-Path $runRoot ('checker-report-{0:D3}.json' -f $script:checkSequence)
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $CheckerPath,
        '-Repository', $Repository,
        '-Mode', $Mode,
        '-ExpectedBranch', $ExpectedBranch,
        '-PrivatePatternsFile', $patternsPath,
        '-ReportPath', $reportPath
    )
    if ($SkipRemoteCheck) {
        $arguments += '-SkipRemoteCheck'
    }
    else {
        $arguments += @('-Remote', 'origin', '-ExpectedRemoteUrl', $ExpectedRemoteUrl)
    }

    $run = Invoke-IsolatedNative -FilePath $shell -Arguments $arguments -ExtraEnvironment $ExtraEnvironment
    $report = $null
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
    }
    [pscustomobject]@{
        ExitCode = $run.ExitCode
        Output = @($run.Output)
        Report = $report
    }
}

function Add-ConformanceCase {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Requirement,
        [Parameter(Mandatory = $true)]
        [ValidateSet('pass', 'blocked')]
        [string]$Expected,
        [string]$ExpectedKind,
        [string]$ExpectedErrorContains,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    try {
        $run = & $Action
        if ($null -eq $run -or $null -eq $run.ExitCode) {
            throw 'Case action did not return a checker result.'
        }

        $violationKinds = @()
        if ($null -ne $run.Report -and $null -ne $run.Report.violations) {
            $violationKinds = @($run.Report.violations | ForEach-Object { [string]$_.kind } | Sort-Object -Unique)
        }
        $actual = if ($run.ExitCode -eq 0) {
            'pass'
        }
        elseif ($null -ne $run.Report -and $run.Report.status -eq 'blocked') {
            'blocked'
        }
        else {
            'blocked-error'
        }

        $passed = if ($Expected -eq 'pass') {
            $run.ExitCode -eq 0 -and $null -ne $run.Report -and $run.Report.status -eq 'passed'
        }
        else {
            $matched = $run.ExitCode -ne 0
            if ($ExpectedKind) {
                $matched = $matched -and ($violationKinds -contains $ExpectedKind)
            }
            if ($ExpectedErrorContains) {
                $matched = $matched -and (($run.Output -join "`n").Contains($ExpectedErrorContains, [StringComparison]::Ordinal))
            }
            $matched
        }

        $kindSummary = if ($violationKinds.Count -gt 0) { $violationKinds -join ',' } else { '<none>' }
        $detail = if ($null -ne $run.Report) {
            'checker status={0}; violations={1}' -f $run.Report.status, $kindSummary
        }
        else {
            (($run.Output | Select-Object -Last 3) -join ' | ')
        }
        $results.Add([pscustomobject]@{
            name = $Name
            requirement = $Requirement
            expected = $Expected
            actual = $actual
            passed = [bool]$passed
            violationKinds = @($violationKinds)
            scannedEntryCount = if ($null -ne $run.Report) { [int]$run.Report.scannedEntryCount } else { $null }
            scannedCommitCount = if ($null -ne $run.Report) { [int]$run.Report.scannedCommitCount } else { $null }
            outgoingCommitCount = if ($null -ne $run.Report) { [int]$run.Report.outgoingCommitCount } else { $null }
            detail = $detail
        })
    }
    catch {
        $results.Add([pscustomobject]@{
            name = $Name
            requirement = $Requirement
            expected = $Expected
            actual = 'harness-error'
            passed = $false
            violationKinds = @()
            scannedEntryCount = $null
            scannedCommitCount = $null
            outgoingCommitCount = $null
            detail = $_.Exception.Message
        })
    }
}

try {
    Add-ConformanceCase -Name 'staged-index-safe-worktree-private' `
        -Requirement 'Staged mode reads the index blob, not later unstaged worktree content.' `
        -Expected pass -Action {
            $repo = New-SyntheticRepository -Name 'staged-index-safe'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'host\service.txt') -Value 'Synthetic public index content.' -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/service.txt')
            Set-Content -LiteralPath (Join-Path $repo 'host\service.txt') -Value "Unstaged $privateLiteral content." -Encoding utf8
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'staged-index-private-worktree-safe' `
        -Requirement 'Staged mode blocks private index content even when the worktree was overwritten with safe text.' `
        -Expected blocked -ExpectedKind 'private-literal' -Action {
            $repo = New-SyntheticRepository -Name 'staged-index-private'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'host\service.txt') -Value "Staged $privateCodeMarker content." -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/service.txt')
            Set-Content -LiteralPath (Join-Path $repo 'host\service.txt') -Value 'Synthetic public worktree content.' -Encoding utf8
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'outgoing-private-then-deleted' `
        -Requirement 'Outgoing mode scans every commit after the destination base, including material deleted by HEAD.' `
        -Expected blocked -ExpectedKind 'private-literal' -Action {
            $repo = New-SyntheticRepository -Name 'outgoing-history'
            Add-BaselineCommit -Repository $repo
            $remote = New-LocalBareRemote -Name 'outgoing-history-remote.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $remote
            New-Item -ItemType Directory -Path (Join-Path $repo 'docs') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'docs\temporary.txt') -Value "Temporary $privateLiteral reference." -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'docs/temporary.txt')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Add temporary synthetic reference')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('rm', '--', 'docs/temporary.txt')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Remove temporary synthetic reference')
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $remote
        }

    Add-ConformanceCase -Name 'ascii-rename-private-index' `
        -Requirement 'A staged rename is scanned at its destination using the staged blob.' `
        -Expected blocked -ExpectedKind 'private-literal' -Action {
            $repo = New-SyntheticRepository -Name 'ascii-rename-private'
            New-Item -ItemType Directory -Path (Join-Path $repo 'docs') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'docs\before.txt') -Value 'Synthetic public content.' -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'docs/before.txt')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Add rename baseline')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('mv', '--', 'docs/before.txt', 'docs/after.txt')
            Set-Content -LiteralPath (Join-Path $repo 'docs\after.txt') -Value "Renamed $privateCodeMarker content." -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'docs/after.txt')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'unicode-rename-safe' `
        -Requirement 'A clean staged rename with a non-ASCII path remains scannable and allowed.' `
        -Expected pass -Action {
            $repo = New-SyntheticRepository -Name 'unicode-rename-safe'
            New-Item -ItemType Directory -Path (Join-Path $repo 'docs') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'docs\plain.txt') -Value 'Synthetic public content.' -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'docs/plain.txt')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Add unicode rename baseline')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('mv', '--', 'docs/plain.txt', 'docs/naïve-例.txt')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'binary-no-nul-unblocked-extension' `
        -Requirement 'Unknown binary content is blocked even when its extension is not on the static denylist.' `
        -Expected blocked -ExpectedKind 'binary-content' -Action {
            $repo = New-SyntheticRepository -Name 'binary-no-nul'
            New-Item -ItemType Directory -Path (Join-Path $repo 'docs') | Out-Null
            [IO.File]::WriteAllBytes((Join-Path $repo 'docs\payload.dat'), [byte[]](0x89, 0x42, 0x49, 0x4e, 0x0d, 0x0a, 0x1a, 0x0a, 0xff, 0xfe, 0xfd, 0xfc))
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'docs/payload.dat')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'added-symlink-mode' `
        -Requirement 'A newly staged symbolic-link entry is blocked by Git mode without following a link.' `
        -Expected blocked -ExpectedKind 'unsupported-git-mode' -Action {
            $repo = New-SyntheticRepository -Name 'added-symlink'
            Add-BaselineCommit -Repository $repo
            Set-Content -LiteralPath (Join-Path $repo 'synthetic-link-target.txt') -Value 'docs/target.txt' -Encoding ascii
            $blob = @(Invoke-TestGit -Repository $repo -GitArguments @('hash-object', '-w', '--', 'synthetic-link-target.txt'))[0]
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-index', '--add', '--cacheinfo', "120000,$blob,host/link-ref")
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'typechange-to-symlink-mode' `
        -Requirement 'A staged regular-file to symbolic-link type change is included in the staged scan.' `
        -Expected blocked -ExpectedKind 'unsupported-git-mode' -Action {
            $repo = New-SyntheticRepository -Name 'typechange-symlink'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            Set-Content -LiteralPath (Join-Path $repo 'host\link-ref') -Value 'Synthetic public regular file.' -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/link-ref')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Add regular-file baseline')
            Set-Content -LiteralPath (Join-Path $repo 'synthetic-link-target.txt') -Value 'docs/target.txt' -Encoding ascii
            $blob = @(Invoke-TestGit -Repository $repo -GitArguments @('hash-object', '-w', '--', 'synthetic-link-target.txt'))[0]
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-index', '--cacheinfo', "120000,$blob,host/link-ref")
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'added-gitlink-mode' `
        -Requirement 'A newly staged gitlink/submodule entry is blocked by Git mode.' `
        -Expected blocked -ExpectedKind 'unsupported-git-mode' -Action {
            $repo = New-SyntheticRepository -Name 'added-gitlink'
            Add-BaselineCommit -Repository $repo
            $commit = @(Invoke-TestGit -Repository $repo -GitArguments @('rev-parse', 'HEAD'))[0]
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-index', '--add', '--cacheinfo', "160000,$commit,unity-package/dependency")
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'lfs-pointer' `
        -Requirement 'A Git LFS pointer is blocked even though the pointer itself is text.' `
        -Expected blocked -ExpectedKind 'lfs-pointer' -Action {
            $repo = New-SyntheticRepository -Name 'lfs-pointer'
            New-Item -ItemType Directory -Path (Join-Path $repo 'unity-package') | Out-Null
            $pointerPrefix = 'version https://' + 'git-lfs.github.com/spec/v1'
            $pointer = @($pointerPrefix, ('oid sha256:' + ('a' * 64)), 'size 12') -join "`n"
            Set-Content -LiteralPath (Join-Path $repo 'unity-package\payload.asset') -Value $pointer -Encoding ascii
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'unity-package/payload.asset')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'alternates-file' `
        -Requirement 'A repository with an objects/info/alternates file is rejected before content scanning.' `
        -Expected blocked -ExpectedErrorContains 'Git object alternates are not allowed.' -Action {
            $donor = New-SyntheticRepository -Name 'alternates-donor'
            Add-BaselineCommit -Repository $donor
            $repo = New-SyntheticRepository -Name 'alternates-consumer'
            Add-BaselineCommit -Repository $repo
            $alternatesPath = Join-Path $repo '.git\objects\info\alternates'
            $donorObjects = (Join-Path $donor '.git\objects').Replace('\', '/')
            Set-Content -LiteralPath $alternatesPath -Value $donorObjects -Encoding ascii
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'environment-alternate-object-reliance' `
        -Requirement 'A repository whose HEAD is resolvable only through GIT_ALTERNATE_OBJECT_DIRECTORIES is rejected.' `
        -Expected blocked -Action {
            $donor = New-SyntheticRepository -Name 'environment-alternate-donor'
            Add-BaselineCommit -Repository $donor
            $donorCommit = @(Invoke-TestGit -Repository $donor -GitArguments @('rev-parse', 'HEAD'))[0]
            $alternateObjects = [IO.Path]::GetFullPath((Join-Path $donor '.git\objects'))
            $repo = New-SyntheticRepository -Name 'environment-alternate-consumer'
            $alternateEnvironment = @{ GIT_ALTERNATE_OBJECT_DIRECTORIES = $alternateObjects }
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-ref', 'refs/heads/feature/test', $donorCommit) -ExtraEnvironment $alternateEnvironment
            $remote = New-LocalBareRemote -Name 'environment-alternate-remote.git'
            $null = Invoke-TestGit -Repository $repo -GitArguments @('remote', 'add', 'origin', $remote)
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $remote -ExtraEnvironment $alternateEnvironment
        }

    Add-ConformanceCase -Name 'linked-worktree-shared-object-dir' `
        -Requirement 'A linked worktree sharing a common Git object directory is rejected.' `
        -Expected blocked -ExpectedErrorContains 'Shared Git object directories are not allowed.' -Action {
            $main = New-SyntheticRepository -Name 'shared-worktree-main'
            Add-BaselineCommit -Repository $main
            $linked = Join-Path $runRoot 'shared-worktree-linked'
            $null = Invoke-TestGit -Repository $main -GitArguments @('worktree', 'add', '-b', 'feature/shared', $linked, 'HEAD')
            Invoke-PublicationChecker -Repository $linked -Mode outgoing -ExpectedBranch 'feature/shared' -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'quoted-api-key-shape' `
        -Requirement 'A quoted credential-like assignment is blocked using only a clearly fake value.' `
        -Expected blocked -ExpectedKind 'credential-shape' -Action {
            $repo = New-SyntheticRepository -Name 'quoted-credential'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            $credentialName = 'api' + '_key'
            $fakeValue = 'FAKE_ONLY_' + 'NOT_A_SECRET_1234567890'
            Set-Content -LiteralPath (Join-Path $repo 'host\settings.txt') -Value ($credentialName + ' = "' + $fakeValue + '"') -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/settings.txt')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'unquoted-api-key-shape' `
        -Requirement 'An unquoted credential-like assignment is blocked using only a clearly fake value.' `
        -Expected blocked -ExpectedKind 'credential-shape' -Action {
            $repo = New-SyntheticRepository -Name 'unquoted-credential'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            $credentialName = 'api' + '_key'
            $fakeValue = 'FAKE_ONLY_' + 'NOT_A_SECRET_1234567890'
            Set-Content -LiteralPath (Join-Path $repo 'host\settings.txt') -Value ($credentialName + '=' + $fakeValue) -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/settings.txt')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'unquoted-lowercase-password-shape' `
        -Requirement 'An unquoted lowercase credential-like assignment is blocked using only a clearly fake value.' `
        -Expected blocked -ExpectedKind 'credential-shape' -Action {
            $repo = New-SyntheticRepository -Name 'unquoted-lowercase-credential'
            New-Item -ItemType Directory -Path (Join-Path $repo 'host') | Out-Null
            $credentialName = 'pass' + 'word'
            $fakeValue = 'syntheticlowercaseonlyvalue'
            Set-Content -LiteralPath (Join-Path $repo 'host\settings.txt') -Value ($credentialName + '=' + $fakeValue) -Encoding utf8
            $null = Invoke-TestGit -Repository $repo -GitArguments @('add', '--', 'host/settings.txt')
            Invoke-PublicationChecker -Repository $repo -Mode staged -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'outgoing-same-blob-mode-transition' `
        -Requirement 'Outgoing history scans a disallowed mode even when a newer commit has the same blob and path in a regular-file mode.' `
        -Expected blocked -ExpectedKind 'unsupported-git-mode' -Action {
            $repo = New-SyntheticRepository -Name 'outgoing-mode-transition'
            Add-BaselineCommit -Repository $repo
            $remote = New-LocalBareRemote -Name 'outgoing-mode-transition.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $remote
            Set-Content -LiteralPath (Join-Path $repo 'synthetic-link-target.txt') -Value 'docs/target.txt' -Encoding ascii -NoNewline
            $blob = @(Invoke-TestGit -Repository $repo -GitArguments @('hash-object', '-w', '--', 'synthetic-link-target.txt'))[0]
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-index', '--add', '--cacheinfo', "120000,$blob,host/link-ref")
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Add synthetic link mode')
            $null = Invoke-TestGit -Repository $repo -GitArguments @('update-index', '--cacheinfo', "100644,$blob,host/link-ref")
            $null = Invoke-TestGit -Repository $repo -GitArguments @('commit', '-m', 'Restore synthetic regular mode')
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $remote
        }

    Add-ConformanceCase -Name 'exact-local-remote-binding' `
        -Requirement 'An exact current branch plus one matching fetch/push URL and local destination ref passes.' `
        -Expected pass -Action {
            $repo = New-SyntheticRepository -Name 'exact-remote-binding'
            Add-BaselineCommit -Repository $repo
            $remote = New-LocalBareRemote -Name 'exact-remote-binding.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $remote
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $remote
        }

    Add-ConformanceCase -Name 'fetch-url-mismatch' `
        -Requirement 'A configured fetch URL that differs from ExpectedRemoteUrl is rejected.' `
        -Expected blocked -ExpectedErrorContains 'Fetch URL set does not exactly match ExpectedRemoteUrl.' -Action {
            $repo = New-SyntheticRepository -Name 'fetch-url-mismatch'
            Add-BaselineCommit -Repository $repo
            $actualRemote = New-LocalBareRemote -Name 'fetch-url-actual.git'
            $expectedRemote = New-LocalBareRemote -Name 'fetch-url-expected.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $actualRemote
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $expectedRemote
        }

    Add-ConformanceCase -Name 'push-url-mismatch' `
        -Requirement 'A push URL that differs from the exact expected fetch URL is rejected.' `
        -Expected blocked -ExpectedErrorContains 'Push URL set does not exactly match ExpectedRemoteUrl.' -Action {
            $repo = New-SyntheticRepository -Name 'push-url-mismatch'
            Add-BaselineCommit -Repository $repo
            $fetchRemote = New-LocalBareRemote -Name 'push-url-fetch.git'
            $pushRemote = New-LocalBareRemote -Name 'push-url-other.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $fetchRemote
            $null = Invoke-TestGit -Repository $repo -GitArguments @('remote', 'set-url', '--add', '--push', 'origin', $pushRemote)
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $fetchRemote
        }

    Add-ConformanceCase -Name 'branch-mismatch' `
        -Requirement 'The current branch must exactly equal ExpectedBranch even when remote checks are skipped.' `
        -Expected blocked -ExpectedErrorContains 'Current branch does not match ExpectedBranch.' -Action {
            $repo = New-SyntheticRepository -Name 'branch-mismatch'
            Add-BaselineCommit -Repository $repo
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedBranch 'feature/different' -SkipRemoteCheck
        }

    Add-ConformanceCase -Name 'active-url-rewrite' `
        -Requirement 'Active Git URL rewrite rules are rejected even when the configured local remote URL matches.' `
        -Expected blocked -ExpectedErrorContains 'Git URL rewrite rules are active.' -Action {
            $repo = New-SyntheticRepository -Name 'active-url-rewrite'
            Add-BaselineCommit -Repository $repo
            $remote = New-LocalBareRemote -Name 'active-url-rewrite.git'
            Connect-And-PushBaseline -Repository $repo -RemotePath $remote
            $null = Invoke-TestGit -Repository $repo -GitArguments @('config', 'url.synthetic-mapped.invalid:.insteadOf', 'synthetic-alias.invalid:')
            Invoke-PublicationChecker -Repository $repo -Mode outgoing -ExpectedRemoteUrl $remote
        }

    $passedCount = @($results | Where-Object { $_.passed }).Count
    $failedCount = $results.Count - $passedCount
    $summary = [ordered]@{
        status = if ($failedCount -eq 0) { 'passed' } else { 'failed' }
        testCount = $results.Count
        passedCount = $passedCount
        failedCount = $failedCount
        checkerSha256 = (Get-FileHash -LiteralPath $CheckerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        coverage = @(
            'staged index versus worktree content',
            'outgoing history including introduced-then-deleted material',
            'ASCII and non-ASCII staged renames',
            'binary content with an unblocked extension',
            'symlink and gitlink modes including a staged type change',
            'Git LFS pointer text',
            'alternates file, environment alternate reliance, and linked worktree sharing',
            'quoted, unquoted mixed-case, and unquoted lowercase fake credential-like assignments',
            'outgoing same-blob same-path Git mode transitions',
            'exact branch, fetch URL, push URL, destination ref, and URL rewrite binding'
        )
        cases = @($results)
    }
}
finally {
    if (-not $KeepFixtures) {
        $resolvedRunRoot = [IO.Path]::GetFullPath($runRoot).TrimEnd('\', '/')
        $expectedPrefix = $ScratchParent + [IO.Path]::DirectorySeparatorChar
        $leaf = [IO.Path]::GetFileName($resolvedRunRoot)
        if (-not $resolvedRunRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not $leaf.StartsWith('relay-liveloop-publication-conformance-', [StringComparison]::Ordinal)) {
            throw 'Refusing to remove an unexpected synthetic fixture path.'
        }
        if (Test-Path -LiteralPath $resolvedRunRoot) {
            Remove-Item -LiteralPath $resolvedRunRoot -Recurse -Force
        }
    }
}

$json = $summary | ConvertTo-Json -Depth 8
if ($ResultPath) {
    $fullResultPath = [IO.Path]::GetFullPath($ResultPath)
    $resultParent = Split-Path -Parent $fullResultPath
    if (-not (Test-Path -LiteralPath $resultParent -PathType Container)) {
        throw 'ResultPath parent directory does not exist.'
    }
    Set-Content -LiteralPath $fullResultPath -Value $json -Encoding utf8
}
$json
if ($summary.failedCount -gt 0) {
    exit 1
}
