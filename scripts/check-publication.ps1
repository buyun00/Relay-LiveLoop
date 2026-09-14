[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,

    [ValidateSet('staged', 'outgoing', 'all')]
    [string]$Mode = 'all',

    [string]$Remote = 'origin',
    [string]$ExpectedRemoteUrl,
    [string]$ExpectedBranch,
    [string]$PrivatePatternsFile,
    [string]$ProposedMessage,
    [string]$ReportPath,
    [switch]$SkipRemoteCheck,

    [string[]]$AllowedRoots = @(
        '.gitignore', '.gitattributes', 'AGENTS.md', 'README.md', 'pyproject.toml',
        'relay_liveloop.py', '.github', 'api', 'clients', 'contracts', 'docs',
        'host', 'mcp', 'scripts', 'tests', 'unity-package'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$disallowedGitEnvironment = @(
    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_OBJECT_DIRECTORY', 'GIT_COMMON_DIR',
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_SHALLOW_FILE',
    'GIT_REPLACE_REF_BASE'
)
foreach ($name in $disallowedGitEnvironment) {
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
        throw "Git object or worktree override environment is active: $name"
    }
}

$repositoryItem = Get-Item -LiteralPath $Repository -Force -ErrorAction Stop
if (-not $repositoryItem.PSIsContainer) {
    throw 'Repository must be a directory.'
}
$Repository = [IO.Path]::GetFullPath($repositoryItem.FullName).TrimEnd('\')

function Invoke-GitResult {
    param([string[]]$GitArguments)

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = @(& git -C $Repository @GitArguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Lines = @($raw | ForEach-Object { $_.ToString() })
    }
}

function Get-GitLines {
    param([string[]]$GitArguments)

    $result = Invoke-GitResult -GitArguments $GitArguments
    if ($result.ExitCode -ne 0) {
        throw "Git command failed: git $($GitArguments -join ' ')"
    }
    @($result.Lines)
}

function Invoke-GitBytes {
    param([string[]]$GitArguments)

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'git'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.ArgumentList.Add('-C')
    $startInfo.ArgumentList.Add($Repository)
    foreach ($argument in $GitArguments) {
        $startInfo.ArgumentList.Add($argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $memory = [IO.MemoryStream]::new()
    try {
        if (-not $process.Start()) {
            throw 'Unable to start Git for binary-safe object inspection.'
        }
        $copyTask = $process.StandardOutput.BaseStream.CopyToAsync($memory)
        $errorTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $copyTask.GetAwaiter().GetResult() | Out-Null
        $errorText = $errorTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "Git binary-safe inspection failed: $errorText"
        }
        Write-Output -NoEnumerate ([byte[]]$memory.ToArray())
    }
    finally {
        $memory.Dispose()
        $process.Dispose()
    }
}

$strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
function Get-GitNullRecords {
    param([string[]]$GitArguments)

    [byte[]]$bytes = Invoke-GitBytes -GitArguments $GitArguments
    try {
        $content = $strictUtf8.GetString($bytes)
    }
    catch [Text.DecoderFallbackException] {
        throw 'Git returned a path that is not valid UTF-8.'
    }
    if ($content.Length -eq 0) {
        return
    }
    @($content.Split([char]0, [StringSplitOptions]::RemoveEmptyEntries))
}

$violations = [Collections.Generic.List[object]]::new()
function Add-Violation {
    param(
        [string]$Kind,
        [string]$Path,
        [string]$Commit
    )

    $violations.Add([pscustomobject]@{
        kind = $Kind
        path = $Path
        commit = $Commit
    })
}

function Test-AllowedPath {
    param([string]$Path)

    $normalized = $Path.Replace('\', '/').TrimStart('/')
    foreach ($root in $AllowedRoots) {
        $normalizedRoot = $root.Replace('\', '/').Trim('/')
        if ($normalized -eq $normalizedRoot -or $normalized.StartsWith("$normalizedRoot/", [StringComparison]::Ordinal)) {
            return $true
        }
    }
    return $false
}

$literalPatterns = @()
$configuredBlockedExtensions = @()
if ($PrivatePatternsFile) {
    $patternPath = [IO.Path]::GetFullPath($PrivatePatternsFile)
    $patternConfig = Get-Content -LiteralPath $patternPath -Raw | ConvertFrom-Json
    if ($null -ne $patternConfig.literalPatterns) {
        $literalPatterns = @($patternConfig.literalPatterns | ForEach-Object { [string]$_ } | Where-Object { $_.Length -gt 0 })
    }
    if ($null -ne $patternConfig.blockedExtensions) {
        $configuredBlockedExtensions = @($patternConfig.blockedExtensions | ForEach-Object { ([string]$_).ToLowerInvariant() })
    }
}

$blockedExtensions = @(
    '.7z', '.bundle', '.dll', '.exe', '.gif', '.jpeg', '.jpg', '.mov', '.mp3',
    '.mp4', '.pdb', '.png', '.tar', '.unitypackage', '.wav', '.webm', '.webp',
    '.zip'
) + $configuredBlockedExtensions | Sort-Object -Unique

$sensitiveShapes = @(
    '(?i)https?://[^/\s:@]+:[^@\s]+@',
    '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----',
    '(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b',
    '(?i)\bAKIA[0-9A-Z]{16}\b',
    '(?i)(password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[''"][^''"]{8,}[''"]',
    '(?i)\b(password|passwd|api[_-]?key|access[_-]?token|secret)\b\s*[:=]\s*[A-Za-z0-9_-]{8,}'
)

function Scan-Text {
    param(
        [string]$Content,
        [string]$Path,
        [string]$Commit
    )

    foreach ($pattern in $literalPatterns) {
        if ($Content.IndexOf($pattern, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Add-Violation -Kind 'private-literal' -Path $Path -Commit $Commit
            break
        }
    }
    foreach ($shape in $sensitiveShapes) {
        if ($Content -match $shape) {
            Add-Violation -Kind 'credential-shape' -Path $Path -Commit $Commit
            break
        }
    }
    if ($Content.IndexOf("`0", [StringComparison]::Ordinal) -ge 0) {
        Add-Violation -Kind 'binary-content' -Path $Path -Commit $Commit
    }
    $lfsPointerPrefix = 'version https://' + 'git-lfs.github.com/spec'
    if ($Content.Contains($lfsPointerPrefix)) {
        Add-Violation -Kind 'lfs-pointer' -Path $Path -Commit $Commit
    }
}

function Scan-Entry {
    param(
        [string]$ModeValue,
        [string]$Blob,
        [string]$Path,
        [string]$Commit
    )

    if ($ModeValue -notin @('100644', '100755')) {
        Add-Violation -Kind 'unsupported-git-mode' -Path $Path -Commit $Commit
        return
    }
    if (-not (Test-AllowedPath -Path $Path)) {
        Add-Violation -Kind 'path-not-allowed' -Path $Path -Commit $Commit
    }
    if ($Path -eq '.gitmodules') {
        Add-Violation -Kind 'submodule-definition' -Path $Path -Commit $Commit
    }
    $extension = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    if ($blockedExtensions -contains $extension) {
        Add-Violation -Kind 'blocked-extension' -Path $Path -Commit $Commit
    }

    try {
        [byte[]]$blobBytes = Invoke-GitBytes -GitArguments @('cat-file', 'blob', $Blob)
    }
    catch {
        Add-Violation -Kind 'unreadable-blob' -Path $Path -Commit $Commit
        return
    }
    if ($blobBytes.Length -gt 16MB) {
        Add-Violation -Kind 'oversized-blob' -Path $Path -Commit $Commit
        return
    }
    if ($blobBytes -contains 0) {
        Add-Violation -Kind 'binary-content' -Path $Path -Commit $Commit
        return
    }
    try {
        $content = $strictUtf8.GetString($blobBytes)
    }
    catch [Text.DecoderFallbackException] {
        Add-Violation -Kind 'binary-content' -Path $Path -Commit $Commit
        return
    }
    foreach ($value in $blobBytes) {
        if (($value -lt 9) -or ($value -gt 13 -and $value -lt 32) -or $value -eq 127) {
            Add-Violation -Kind 'binary-content' -Path $Path -Commit $Commit
            return
        }
    }
    Scan-Text -Content $content -Path $Path -Commit $Commit
}

$topLines = @(Get-GitLines -GitArguments @('rev-parse', '--show-toplevel'))
$top = [IO.Path]::GetFullPath($topLines[0]).TrimEnd('\')
if (-not $top.Equals($Repository, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Repository path is not the Git worktree root.'
}

$gitDirectoryLines = @(Get-GitLines -GitArguments @('rev-parse', '--absolute-git-dir'))
$gitDirectory = [IO.Path]::GetFullPath($gitDirectoryLines[0])
$commonLines = @(Get-GitLines -GitArguments @('rev-parse', '--git-common-dir'))
$commonRaw = $commonLines[0]
$commonDirectory = if ([IO.Path]::IsPathRooted($commonRaw)) {
    [IO.Path]::GetFullPath($commonRaw)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Repository $commonRaw))
}
if (-not $gitDirectory.Equals($commonDirectory, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Shared Git object directories are not allowed.'
}
if (Test-Path -LiteralPath (Join-Path $gitDirectory 'objects\info\alternates')) {
    throw 'Git object alternates are not allowed.'
}
$replaceRefs = @(Get-GitLines -GitArguments @('for-each-ref', '--format=%(refname)', 'refs/replace/'))
if ($replaceRefs.Count -gt 0) {
    throw 'Git replacement objects are not allowed.'
}

$branchLines = @(Get-GitLines -GitArguments @('branch', '--show-current'))
$currentBranch = $branchLines[0]
if ($ExpectedBranch -and $currentBranch -ne $ExpectedBranch) {
    throw 'Current branch does not match ExpectedBranch.'
}

$remoteBase = $null
if (-not $SkipRemoteCheck) {
    if (-not $ExpectedRemoteUrl) {
        throw 'ExpectedRemoteUrl is required unless SkipRemoteCheck is set.'
    }
    if (-not $ExpectedBranch) {
        throw 'ExpectedBranch is required unless SkipRemoteCheck is set.'
    }

    $fetchUrls = @(Get-GitLines -GitArguments @('remote', 'get-url', '--all', $Remote))
    $pushUrls = @(Get-GitLines -GitArguments @('remote', 'get-url', '--push', '--all', $Remote))
    if ($fetchUrls.Count -ne 1 -or $fetchUrls[0] -ne $ExpectedRemoteUrl) {
        throw 'Fetch URL set does not exactly match ExpectedRemoteUrl.'
    }
    if ($pushUrls.Count -ne 1 -or $pushUrls[0] -ne $ExpectedRemoteUrl) {
        throw 'Push URL set does not exactly match ExpectedRemoteUrl.'
    }

    $rewriteCheck = Invoke-GitResult -GitArguments @('config', '--show-origin', '--get-regexp', '^url\..*\.(insteadof|pushinsteadof)$')
    if ($rewriteCheck.ExitCode -eq 0 -and $rewriteCheck.Lines.Count -gt 0) {
        throw 'Git URL rewrite rules are active.'
    }
    if ($rewriteCheck.ExitCode -notin @(0, 1)) {
        throw 'Unable to inspect Git URL rewrite rules.'
    }

    $remoteResult = Invoke-GitResult -GitArguments @('ls-remote', '--heads', $Remote, "refs/heads/$ExpectedBranch")
    if ($remoteResult.ExitCode -ne 0) {
        throw 'Unable to read the destination branch.'
    }
    if ($remoteResult.Lines.Count -gt 1) {
        throw 'Destination branch lookup returned multiple refs.'
    }
    if ($remoteResult.Lines.Count -eq 1 -and $remoteResult.Lines[0] -match '^([0-9a-f]{40})\s+') {
        $remoteBase = $Matches[1]
        $objectCheck = Invoke-GitResult -GitArguments @('cat-file', '-e', "$remoteBase^{commit}")
        if ($objectCheck.ExitCode -ne 0) {
            throw 'Destination commit is not available in the local object database.'
        }
        $ancestorCheck = Invoke-GitResult -GitArguments @('merge-base', '--is-ancestor', $remoteBase, 'HEAD')
        if ($ancestorCheck.ExitCode -ne 0) {
            throw 'Destination branch is not an ancestor of HEAD; push would not be fast-forward.'
        }
    }
}

$reparsePoints = @(Get-ChildItem -LiteralPath $Repository -Recurse -Force -ErrorAction Stop |
    Where-Object {
        -not $_.FullName.StartsWith($gitDirectory, [StringComparison]::OrdinalIgnoreCase) -and
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
    })
foreach ($point in $reparsePoints) {
    $relative = $point.FullName.Substring($Repository.Length).TrimStart('\').Replace('\', '/')
    Add-Violation -Kind 'reparse-point' -Path $relative -Commit $null
}

$scannedEntries = 0
$scannedCommits = 0
$outgoingCommits = @()

if ($Mode -in @('staged', 'all')) {
    foreach ($indexEntry in @(Get-GitNullRecords -GitArguments @('ls-files', '-s', '-z'))) {
        if ($indexEntry -notmatch '^(\d{6}) ([0-9a-f]+) (\d+)\t([\s\S]+)$') {
            Add-Violation -Kind 'invalid-index-entry' -Path $indexEntry -Commit $null
            continue
        }
        $modeValue = $Matches[1]
        $blob = $Matches[2]
        $stage = $Matches[3]
        $path = $Matches[4]
        if ($stage -ne '0') {
            Add-Violation -Kind 'unmerged-index-entry' -Path $path -Commit $null
            continue
        }
        Scan-Entry -ModeValue $modeValue -Blob $blob -Path $path -Commit $null
        $scannedEntries++
    }
}

if ($Mode -in @('outgoing', 'all')) {
    $rangeArguments = if ($remoteBase) { @('rev-list', "$remoteBase..HEAD") } else { @('rev-list', 'HEAD') }
    $outgoingCommits = @(Get-GitLines -GitArguments $rangeArguments)
    $seenEntries = @{}
    foreach ($commit in $outgoingCommits) {
        $message = [string]::Join("`n", (Get-GitLines -GitArguments @('show', '-s', '--format=%B', $commit)))
        Scan-Text -Content $message -Path '<commit-message>' -Commit $commit
        $scannedCommits++

        foreach ($entry in @(Get-GitNullRecords -GitArguments @('ls-tree', '-r', '-z', $commit))) {
            if ($entry -notmatch '^(\d{6}) ([^ ]+) ([0-9a-f]+)\t([\s\S]+)$') {
                Add-Violation -Kind 'unsupported-tree-entry' -Path $entry -Commit $commit
                continue
            }
            $modeValue = $Matches[1]
            $objectType = $Matches[2]
            $blob = $Matches[3]
            $path = $Matches[4]
            if ($objectType -ne 'blob' -and $modeValue -in @('100644', '100755')) {
                Add-Violation -Kind 'unsupported-tree-entry' -Path $path -Commit $commit
                continue
            }
            $key = "$modeValue`t$objectType`t$blob`t$path"
            if (-not $seenEntries.ContainsKey($key)) {
                $seenEntries[$key] = $true
                Scan-Entry -ModeValue $modeValue -Blob $blob -Path $path -Commit $commit
                $scannedEntries++
            }
        }
    }
}

if ($ProposedMessage) {
    Scan-Text -Content $ProposedMessage -Path '<proposed-message>' -Commit $null
}

$result = [ordered]@{
    status = if ($violations.Count -eq 0) { 'passed' } else { 'blocked' }
    branch = $currentBranch
    mode = $Mode
    destinationCommit = $remoteBase
    outgoingCommitCount = $outgoingCommits.Count
    scannedCommitCount = $scannedCommits
    scannedEntryCount = $scannedEntries
    privatePatternCount = $literalPatterns.Count
    violationCount = $violations.Count
    violations = @($violations)
}
$json = $result | ConvertTo-Json -Depth 5

if ($ReportPath) {
    $fullReportPath = [IO.Path]::GetFullPath($ReportPath)
    $reportParent = Split-Path -Parent $fullReportPath
    if (-not (Test-Path -LiteralPath $reportParent -PathType Container)) {
        throw 'Report parent directory does not exist.'
    }
    Set-Content -LiteralPath $fullReportPath -Value $json -Encoding utf8
}

$json
if ($violations.Count -gt 0) {
    throw "Publication check blocked $($violations.Count) violation(s)."
}
