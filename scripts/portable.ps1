[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('Export', 'Import')][string]$Mode,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [string]$ProjectConfigPath,
    [string[]]$DependencyLockPath = @(),
    [string[]]$BaselinePath = @(),
    [string[]]$ResourcePath = @(),
    [string[]]$DiscoveryRoot = @(),
    [string]$NewDataRoot,
    [string]$PythonExecutable,
    [string]$UnityExecutable,
    [string[]]$SdkRoot = @(),
    [string]$TokenFile,
    [ValidateRange(1, 8)][int]$MaxDiscoveryDepth = 5
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib\Deployment.Common.ps1')

$script:PortableFormat = 'relay-liveloop-portable'
$script:PortableVersion = 1
$script:BlockedDirectoryNames = @(
    '.git', '.venv', 'venv', '__pycache__', '.pytest_cache',
    'Library', 'Temp', 'Obj', 'Logs', 'UserSettings',
    'deployment', 'host', 'portable', 'process-state', 'processes', 'sessions', 'tasks', 'runtime-data', 'cache', 'caches'
)

function Get-PortableRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = (Get-RelayFullPath -Path $Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $pathFull = Get-RelayFullPath -Path $Path
    $rootUri = [Uri]::new($rootFull)
    $pathUri = [Uri]::new($pathFull)
    $relative = [Uri]::UnescapeDataString($rootUri.MakeRelativeUri($pathUri).ToString()).Replace('/', [IO.Path]::DirectorySeparatorChar)
    if ([IO.Path]::IsPathRooted($relative) -or $relative -eq '..' -or $relative.StartsWith('..' + [IO.Path]::DirectorySeparatorChar)) {
        throw "Path escapes its declared root: $pathFull"
    }
    return $relative
}

function Test-PortableBlockedRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $segments = $RelativePath.Replace('/', '\').Split('\', [StringSplitOptions]::RemoveEmptyEntries)
    foreach ($segment in $segments) {
        if ($script:BlockedDirectoryNames -contains $segment) {
            return $true
        }
    }
    $leaf = if ($segments.Count -gt 0) { $segments[$segments.Count - 1] } else { '' }
    if ($leaf -match '(?i)(^|[._-])(token|secret|credential|license)([._-]|$)') {
        return $true
    }
    if ([IO.Path]::GetExtension($leaf) -in @('.pid', '.sqlite', '.sqlite3', '.db', '.wal', '.shm')) {
        return $true
    }
    return $false
}

function Get-PortableFiles {
    param([Parameter(Mandatory = $true)][string]$SelectedPath)

    $selected = Get-RelayExistingPath -Path $SelectedPath
    if (Test-RelayReparsePoint -Path $selected) {
        throw "Reparse points cannot be exported: $selected"
    }
    $item = Get-Item -LiteralPath $selected -Force
    if (-not $item.PSIsContainer) {
        return @($item)
    }
    $queue = [Collections.Generic.Queue[IO.DirectoryInfo]]::new()
    $queue.Enqueue([IO.DirectoryInfo]$item)
    $files = [Collections.Generic.List[IO.FileInfo]]::new()
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points cannot be exported: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $queue.Enqueue([IO.DirectoryInfo]$child)
            }
            else {
                $files.Add([IO.FileInfo]$child)
            }
        }
    }
    return @($files)
}

function Get-PortableRootRole {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$AllowedRoles
    )

    $roots = @(
        [pscustomobject]@{ role = 'gameProject'; path = [string]$Config.gameProjectRoot },
        [pscustomobject]@{ role = 'gameRepo'; path = [string]$Config.gameRepoRoot },
        [pscustomobject]@{ role = 'toolRepo'; path = [string]$Config.toolRepoRoot },
        [pscustomobject]@{ role = 'dataRoot'; path = [string]$Config.dataRoot }
    ) | Where-Object { $_.role -in $AllowedRoles } | Sort-Object { $_.path.Length } -Descending
    foreach ($candidate in $roots) {
        if (Test-RelayPathContained -Parent $candidate.path -Candidate $Path) {
            return $candidate
        }
    }
    throw 'Selected portable input is outside every allowed configured root.'
}

function Add-PortableSelection {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][Collections.Generic.List[object]]$Entries,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][Collections.Generic.HashSet[string]]$Destinations,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$Category,
        [Parameter(Mandatory = $true)][string]$SelectedPath,
        [Parameter(Mandatory = $true)][string[]]$AllowedRoles,
        [Parameter(Mandatory = $true)][int]$SelectionIndex
    )

    $selected = Get-RelayExistingPath -Path $SelectedPath
    $selectedItem = Get-Item -LiteralPath $selected -Force
    $root = Get-PortableRootRole -Config $Config -Path $selected -AllowedRoles $AllowedRoles
    $selectedRelative = Get-PortableRelativePath -Root $root.path -Path $selected
    if (Test-PortableBlockedRelativePath -RelativePath $selectedRelative) {
        throw "Selected portable input is runtime state, cache, or sensitive material: $selectedRelative"
    }
    if ($selectedItem.PSIsContainer -and [string]::IsNullOrWhiteSpace($selectedRelative)) {
        throw 'Exporting an entire configured root is not allowed; select an immutable baseline or resource subtree explicitly.'
    }

    foreach ($file in @(Get-PortableFiles -SelectedPath $selected)) {
        $relativeToRoot = Get-PortableRelativePath -Root $root.path -Path $file.FullName
        if (Test-PortableBlockedRelativePath -RelativePath $relativeToRoot) {
            throw "Portable selection contains runtime state, cache, or sensitive material: $relativeToRoot"
        }
        if ([string]::Equals((Get-RelayFullPath -Path $file.FullName), (Get-RelayFullPath -Path ([string]$Config.tokenFile)), [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The configured token file cannot be exported.'
        }
        $destinationKey = $root.role + ':' + $relativeToRoot.Replace('\', '/').ToLowerInvariant()
        if (-not $Destinations.Add($destinationKey)) {
            throw "Two selections target the same portable destination: $relativeToRoot"
        }
        $relativeWithinSelection = if ($selectedItem.PSIsContainer) {
            Get-PortableRelativePath -Root $selected -Path $file.FullName
        }
        else {
            $file.Name
        }
        $archiveRelative = ('payload/{0}/{1:D3}/{2}' -f $Category, $SelectionIndex, $relativeWithinSelection.Replace('\', '/'))
        $stageFile = Join-Path $StageRoot $archiveRelative.Replace('/', '\')
        [IO.Directory]::CreateDirectory((Split-Path -Parent $stageFile)) | Out-Null
        $sourceLengthBefore = [long]$file.Length
        $sourceHashBefore = Get-RelayFileSha256 -Path $file.FullName
        Copy-Item -LiteralPath $file.FullName -Destination $stageFile
        $sourceItemAfter = Get-Item -LiteralPath $file.FullName
        $sourceHashAfter = Get-RelayFileSha256 -Path $file.FullName
        $stagedHash = Get-RelayFileSha256 -Path $stageFile
        if ([long]$sourceItemAfter.Length -ne $sourceLengthBefore -or $sourceHashAfter -ne $sourceHashBefore -or $stagedHash -ne $sourceHashBefore) {
            throw "Portable input changed while it was being copied: $relativeToRoot"
        }
        $Entries.Add([ordered]@{
            category = $Category
            rootRole = $root.role
            relativePath = $relativeToRoot.Replace('\', '/')
            archivePath = $archiveRelative
            length = $sourceLengthBefore
            sha256 = $stagedHash
        })
    }
}

function Find-PortableRoots {
    param(
        [Parameter(Mandatory = $true)][string[]]$Roots,
        [Parameter(Mandatory = $true)][int]$MaximumDepth
    )

    $toolCandidates = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $projectCandidates = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($rootValue in $Roots) {
        $root = Get-RelayExistingPath -Path $rootValue -Kind Directory
        $queue = [Collections.Generic.Queue[object]]::new()
        $queue.Enqueue([pscustomobject]@{ path = $root; depth = 0 })
        while ($queue.Count -gt 0) {
            $current = $queue.Dequeue()
            if (Test-Path -LiteralPath (Join-Path $current.path 'relay_liveloop.py') -PathType Leaf) {
                [void]$toolCandidates.Add((Get-RelayFullPath -Path $current.path))
            }
            if (Test-Path -LiteralPath (Join-Path $current.path 'ProjectSettings\ProjectVersion.txt') -PathType Leaf) {
                [void]$projectCandidates.Add((Get-RelayFullPath -Path $current.path))
            }
            if ($current.depth -ge $MaximumDepth) {
                continue
            }
            foreach ($child in @(Get-ChildItem -LiteralPath $current.path -Directory -Force -ErrorAction Stop)) {
                if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $script:BlockedDirectoryNames -contains $child.Name) {
                    continue
                }
                $queue.Enqueue([pscustomobject]@{ path = $child.FullName; depth = $current.depth + 1 })
            }
        }
    }
    if ($toolCandidates.Count -ne 1) {
        throw "Root discovery requires exactly one tool repository candidate; found $($toolCandidates.Count)."
    }
    if ($projectCandidates.Count -ne 1) {
        throw "Root discovery requires exactly one Unity project candidate; found $($projectCandidates.Count)."
    }
    $toolRoot = @($toolCandidates)[0]
    $gameProject = @($projectCandidates)[0]
    $currentDirectory = [IO.DirectoryInfo]$gameProject
    $gameRepo = $null
    while ($null -ne $currentDirectory) {
        if (Test-Path -LiteralPath (Join-Path $currentDirectory.FullName '.git')) {
            $gameRepo = $currentDirectory.FullName
            break
        }
        $currentDirectory = $currentDirectory.Parent
    }
    if ($null -eq $gameRepo) {
        throw 'A game repository root containing .git could not be discovered above the Unity project.'
    }
    if ((Test-RelayPathContained -Parent $toolRoot -Candidate $gameRepo) -or (Test-RelayPathContained -Parent $gameRepo -Candidate $toolRoot)) {
        throw 'Discovered tool and game repository roots must be independent.'
    }
    return [pscustomobject]@{ toolRepo = $toolRoot; gameRepo = $gameRepo; gameProject = $gameProject }
}

function Test-PortableManifestRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($normalized) -or [IO.Path]::IsPathRooted($normalized)) {
        return $false
    }
    foreach ($segment in $normalized.Split('\', [StringSplitOptions]::RemoveEmptyEntries)) {
        if ($segment -in @('.', '..')) {
            return $false
        }
    }
    return $true
}

function Expand-PortableArchiveSafely {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Directory]::CreateDirectory($Destination) | Out-Null
    $destinationFull = (Get-RelayFullPath -Path $Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $entryName = $entry.FullName.Replace('/', '\')
            if ([string]::IsNullOrWhiteSpace($entryName)) {
                continue
            }
            if (-not (Test-PortableManifestRelativePath -Path $entryName)) {
                throw "Archive entry is not a confined relative path: $($entry.FullName)"
            }
            $unixMode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            $windowsAttributes = $entry.ExternalAttributes -band 0xFFFF
            if ($unixMode -eq 0xA000 -or (($windowsAttributes -band [int][IO.FileAttributes]::ReparsePoint) -ne 0)) {
                throw "Archive reparse or symbolic-link entry is not allowed: $($entry.FullName)"
            }
            $target = Get-RelayFullPath -Path (Join-Path $Destination $entryName)
            if (-not $target.StartsWith($destinationFull, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive entry escapes the extraction root: $($entry.FullName)"
            }
            if ($entryName.EndsWith('\')) {
                [IO.Directory]::CreateDirectory($target) | Out-Null
                continue
            }
            [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
            $input = $entry.Open()
            $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $input.CopyTo($output)
            }
            finally {
                $output.Dispose()
                $input.Dispose()
            }
        }
    }
    finally {
        $zip.Dispose()
    }
}

function Invoke-PortableExport {
    $configFull = Get-RelayExistingPath -Path $ConfigPath -Kind File
    $config = Get-RelayMachineConfig -ConfigPath $configFull
    Assert-RelayMachineConfigPaths -Config $config -ConfigPath $configFull -RequireUnity
    if ([string]::IsNullOrWhiteSpace($ProjectConfigPath)) {
        throw 'Export requires -ProjectConfigPath.'
    }
    if ($DependencyLockPath.Count -eq 0 -or $BaselinePath.Count -eq 0 -or $ResourcePath.Count -eq 0) {
        throw 'Export requires at least one dependency lock, baseline selection, and resource selection.'
    }
    $archiveFull = Get-RelayFullPath -Path $ArchivePath
    if (Test-Path -LiteralPath $archiveFull) {
        throw "Refusing to overwrite existing portable archive: $archiveFull"
    }
    if (-not (Test-RelayPathContained -Parent ([string]$config.dataRoot) -Candidate $archiveFull)) {
        throw 'Portable archive must be written under the configured dataRoot.'
    }
    Assert-RelayPathOutsideRoots -Path $archiveFull -Roots @([string]$config.toolRepoRoot, [string]$config.gameRepoRoot) -Label 'Portable archive'
    $archiveParent = Split-Path -Parent $archiveFull
    [IO.Directory]::CreateDirectory($archiveParent) | Out-Null

    $bundleId = 'bundle_' + [Guid]::NewGuid().ToString('N')
    $stageRoot = Join-Path ([string]$config.dataRoot) ('portable\staging\' + $bundleId)
    if (Test-Path -LiteralPath $stageRoot) {
        throw 'Generated staging path already exists.'
    }
    [IO.Directory]::CreateDirectory($stageRoot) | Out-Null
    $temporaryArchive = Join-Path $archiveParent ('.' + [IO.Path]::GetFileName($archiveFull) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $entries = [Collections.Generic.List[object]]::new()
        $destinations = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        Add-PortableSelection -Entries $entries -Destinations $destinations -Config $config -StageRoot $stageRoot -Category 'projectConfig' -SelectedPath $ProjectConfigPath -AllowedRoles @('gameProject', 'gameRepo') -SelectionIndex 0
        $index = 0
        foreach ($path in $DependencyLockPath) {
            Add-PortableSelection -Entries $entries -Destinations $destinations -Config $config -StageRoot $stageRoot -Category 'dependencyLock' -SelectedPath $path -AllowedRoles @('toolRepo', 'gameProject', 'gameRepo') -SelectionIndex $index
            $index++
        }
        $index = 0
        foreach ($path in $BaselinePath) {
            Add-PortableSelection -Entries $entries -Destinations $destinations -Config $config -StageRoot $stageRoot -Category 'baseline' -SelectedPath $path -AllowedRoles @('dataRoot') -SelectionIndex $index
            $index++
        }
        $index = 0
        foreach ($path in $ResourcePath) {
            Add-PortableSelection -Entries $entries -Destinations $destinations -Config $config -StageRoot $stageRoot -Category 'resource' -SelectedPath $path -AllowedRoles @('dataRoot') -SelectionIndex $index
            $index++
        }
        $manifest = [ordered]@{
            format = $script:PortableFormat
            schemaVersion = $script:PortableVersion
            bundleId = $bundleId
            createdAtUtc = [DateTime]::UtcNow.ToString('o')
            controlDefaults = [ordered]@{
                address = [string]$config.controlAddress
                controlPort = [int]$config.controlPort
                runtimePort = [int]$config.runtimePort
            }
            unityProjectVersion = [string]$config.unityProjectVersion
            entries = @($entries)
            exclusions = @('process identity', 'live objects', 'sessions', 'task execution state', 'token and license files', 'Library', 'virtual environments', 'caches')
            migrationValidation = 'NOT_RUN'
        }
        Write-RelayJsonFile -Path (Join-Path $stageRoot 'manifest.json') -Value $manifest | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory($stageRoot, $temporaryArchive, [IO.Compression.CompressionLevel]::Optimal, $false)
        Move-Item -LiteralPath $temporaryArchive -Destination $archiveFull
        return [ordered]@{
            status = 'exported'
            bundleId = $bundleId
            archivePath = $archiveFull
            fileCount = $entries.Count
            archiveSha256 = Get-RelayFileSha256 -Path $archiveFull
            exportedLiveState = $false
            exportedCredentialsOrLicenses = $false
            secondMachineValidated = $false
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryArchive) {
            Remove-Item -LiteralPath $temporaryArchive -Force
        }
        if (Test-Path -LiteralPath $stageRoot) {
            if (-not (Test-RelayPathContained -Parent ([string]$config.dataRoot) -Candidate $stageRoot)) {
                throw 'Refusing to clean a staging path outside dataRoot.'
            }
            Remove-Item -LiteralPath $stageRoot -Recurse -Force
        }
    }
}

function Invoke-PortableImport {
    $archiveFull = Get-RelayExistingPath -Path $ArchivePath -Kind File
    $configFull = Get-RelayFullPath -Path $ConfigPath
    if (Test-Path -LiteralPath $configFull) {
        throw "Refusing to overwrite existing machine configuration: $configFull"
    }
    if ($DiscoveryRoot.Count -eq 0 -or [string]::IsNullOrWhiteSpace($NewDataRoot) -or [string]::IsNullOrWhiteSpace($TokenFile)) {
        throw 'Import requires -DiscoveryRoot, -NewDataRoot, and an existing new-machine -TokenFile.'
    }
    $discovered = Find-PortableRoots -Roots $DiscoveryRoot -MaximumDepth $MaxDiscoveryDepth
    $newData = Get-RelayFullPath -Path $NewDataRoot
    Assert-RelayPathOutsideRoots -Path $newData -Roots @($discovered.toolRepo, $discovered.gameRepo) -Label 'New data root'
    Assert-RelayPathOutsideGitTree -Path $newData -Label 'New data root'
    Assert-RelayPathOutsideRoots -Path $configFull -Roots @($discovered.toolRepo, $discovered.gameRepo) -Label 'New machine configuration'
    Assert-RelayPathOutsideGitTree -Path $configFull -Label 'New machine configuration'
    $tokenFull = Get-RelayExistingPath -Path $TokenFile -Kind File
    Assert-RelayPathOutsideRoots -Path $tokenFull -Roots @($discovered.toolRepo, $discovered.gameRepo) -Label 'New-machine token file'
    Assert-RelayPathOutsideGitTree -Path $tokenFull -Label 'New-machine token file'
    Read-RelayToken -TokenFile $tokenFull | Out-Null
    $projectVersion = Get-RelayProjectVersion -GameProjectRoot $discovered.gameProject
    $pythonFull = Resolve-RelayPythonExecutable -ConfiguredPath $PythonExecutable
    $unityFull = Resolve-RelayUnityExecutable -ConfiguredPath $UnityExecutable -RequiredVersion $projectVersion
    $sdkRoots = @($SdkRoot | ForEach-Object { Get-RelayExistingPath -Path $_ -Kind Directory })

    [IO.Directory]::CreateDirectory($newData) | Out-Null
    $extractRoot = Join-Path $newData ('portable\extract-' + [Guid]::NewGuid().ToString('N'))
    try {
        Expand-PortableArchiveSafely -Archive $archiveFull -Destination $extractRoot
        $manifestPath = Get-RelayExistingPath -Path (Join-Path $extractRoot 'manifest.json') -Kind File
        $manifest = Read-RelayJsonFile -Path $manifestPath
        if ($manifest.format -ne $script:PortableFormat -or [int]$manifest.schemaVersion -ne $script:PortableVersion) {
            throw 'Portable manifest format or schemaVersion is unsupported.'
        }
        if ([string]::IsNullOrWhiteSpace([string]$manifest.bundleId)) {
            throw 'Portable manifest bundleId is missing.'
        }
        $receiptRoot = Join-Path $newData ('portable\imports\' + [string]$manifest.bundleId)
        if (Test-Path -LiteralPath $receiptRoot) {
            throw 'Portable bundle receipt already exists; refusing to replace an earlier immutable import record.'
        }
        if (-not [string]::Equals([string]$manifest.unityProjectVersion, $projectVersion, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Discovered Unity project version does not match the portable manifest.'
        }
        $entries = @($manifest.entries)
        if ($entries.Count -eq 0) {
            throw 'Portable manifest contains no files.'
        }
        $archivePaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $destinationPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $planned = [Collections.Generic.List[object]]::new()
        $rootMap = @{
            toolRepo = $discovered.toolRepo
            gameRepo = $discovered.gameRepo
            gameProject = $discovered.gameProject
            dataRoot = $newData
        }
        $projectConfigDestination = $null
        foreach ($entry in $entries) {
            foreach ($name in @('category', 'rootRole', 'relativePath', 'archivePath', 'length', 'sha256')) {
                if ($null -eq $entry.PSObject.Properties[$name]) {
                    throw "Portable manifest entry is missing '$name'."
                }
            }
            $category = [string]$entry.category
            $role = [string]$entry.rootRole
            if ($category -notin @('projectConfig', 'dependencyLock', 'baseline', 'resource')) {
                throw 'Portable manifest entry category is invalid.'
            }
            if (-not $rootMap.ContainsKey($role)) {
                throw 'Portable manifest entry rootRole is invalid.'
            }
            if (($category -in @('baseline', 'resource')) -and $role -ne 'dataRoot') {
                throw 'Baseline and resource entries must target dataRoot.'
            }
            if ($category -eq 'projectConfig' -and $role -notin @('gameRepo', 'gameProject')) {
                throw 'Project configuration entry has an invalid target root.'
            }
            if (-not (Test-PortableManifestRelativePath -Path ([string]$entry.relativePath)) -or -not (Test-PortableManifestRelativePath -Path ([string]$entry.archivePath))) {
                throw 'Portable manifest contains an unsafe relative path.'
            }
            if (Test-PortableBlockedRelativePath -RelativePath ([string]$entry.relativePath)) {
                throw 'Portable manifest attempts to restore blocked runtime, cache, or sensitive material.'
            }
            if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$' -or [long]$entry.length -lt 0) {
                throw 'Portable manifest hash or length is invalid.'
            }
            if (-not $archivePaths.Add(([string]$entry.archivePath).ToLowerInvariant())) {
                throw 'Portable manifest repeats an archive path.'
            }
            $source = Get-RelayFullPath -Path (Join-Path $extractRoot ([string]$entry.archivePath).Replace('/', '\'))
            if (-not (Test-RelayPathContained -Parent $extractRoot -Candidate $source) -or -not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw 'Portable payload file is missing or escapes extraction root.'
            }
            $sourceItem = Get-Item -LiteralPath $source
            if ([long]$sourceItem.Length -ne [long]$entry.length -or (Get-RelayFileSha256 -Path $source) -ne [string]$entry.sha256) {
                throw 'Portable payload hash or length does not match its manifest.'
            }
            $destination = Get-RelayFullPath -Path (Join-Path $rootMap[$role] ([string]$entry.relativePath).Replace('/', '\'))
            if (-not (Test-RelayPathContained -Parent $rootMap[$role] -Candidate $destination)) {
                throw 'Portable destination escapes its discovered root.'
            }
            if (-not $destinationPaths.Add($destination)) {
                throw 'Portable manifest repeats a destination path.'
            }
            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                $existing = Get-Item -LiteralPath $destination
                if ([long]$existing.Length -ne [long]$entry.length -or (Get-RelayFileSha256 -Path $destination) -ne [string]$entry.sha256) {
                    throw "Import refuses to overwrite a different existing file: $destination"
                }
                $action = 'reused_matching'
            }
            elseif (Test-Path -LiteralPath $destination) {
                throw "Portable destination exists but is not a regular file: $destination"
            }
            else {
                $action = 'copy_new'
            }
            if ($category -eq 'projectConfig') {
                if ($null -ne $projectConfigDestination -and -not [string]::Equals($projectConfigDestination, $destination, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Portable manifest contains more than one project configuration destination.'
                }
                $projectConfigDestination = $destination
            }
            $planned.Add([pscustomobject]@{ source = $source; destination = $destination; action = $action; sha256 = [string]$entry.sha256 })
        }
        if ($null -eq $projectConfigDestination) {
            throw 'Portable manifest contains no project configuration.'
        }
        foreach ($requiredCategory in @('dependencyLock', 'baseline', 'resource')) {
            if (@($entries | Where-Object { $_.category -eq $requiredCategory }).Count -eq 0) {
                throw "Portable manifest contains no $requiredCategory entries."
            }
        }
        $allExtractedFiles = @(Get-ChildItem -LiteralPath $extractRoot -File -Recurse -Force | ForEach-Object {
            (Get-PortableRelativePath -Root $extractRoot -Path $_.FullName).Replace('\', '/').ToLowerInvariant()
        })
        $actualPayload = @($allExtractedFiles | Where-Object { $_ -ne 'manifest.json' })
        if ($actualPayload.Count -ne $archivePaths.Count -or @($actualPayload | Where-Object { -not $archivePaths.Contains($_) }).Count -ne 0) {
            throw 'Portable archive contains unmanifested or missing payload files.'
        }

        foreach ($item in $planned) {
            if ($item.action -eq 'copy_new') {
                [IO.Directory]::CreateDirectory((Split-Path -Parent $item.destination)) | Out-Null
                Copy-Item -LiteralPath $item.source -Destination $item.destination
                if ((Get-RelayFileSha256 -Path $item.destination) -ne $item.sha256) {
                    throw 'A restored file failed its post-copy hash check.'
                }
            }
        }

        $stateRoot = Join-Path $newData 'deployment\process-state'
        $databasePath = Join-Path $newData 'host\relay-liveloop.sqlite3'
        $artifactRoot = Join-Path $newData 'artifacts'
        foreach ($directory in @($stateRoot, (Split-Path -Parent $databasePath), $artifactRoot)) {
            [IO.Directory]::CreateDirectory($directory) | Out-Null
        }
        $machine = [ordered]@{
            configKind = $script:RelayMachineConfigKind
            schemaVersion = $script:RelayMachineConfigVersion
            generatedAtUtc = [DateTime]::UtcNow.ToString('o')
            toolRepoRoot = $discovered.toolRepo
            gameRepoRoot = $discovered.gameRepo
            gameProjectRoot = $discovered.gameProject
            dataRoot = $newData
            unityExecutable = $unityFull
            unityProjectVersion = $projectVersion
            pythonExecutable = $pythonFull
            sdkRoots = @($sdkRoots)
            projectConfigFile = $projectConfigDestination
            controlAddress = [string]$manifest.controlDefaults.address
            controlPort = [int]$manifest.controlDefaults.controlPort
            runtimePort = [int]$manifest.controlDefaults.runtimePort
            tokenFile = $tokenFull
            interactiveDesktopVerified = $false
            renderAfterRdpDisconnect = 'UNVERIFIED'
            pocoConnection = $null
            lifecycle = [ordered]@{
                stateRoot = $stateRoot
                databasePath = $databasePath
                artifactRoots = @($artifactRoot)
            }
            migration = [ordered]@{
                newMachineValidation = 'NOT_RUN'
                rootsRediscovered = $true
                importedBundleId = [string]$manifest.bundleId
                importedManifestSha256 = Get-RelayFileSha256 -Path $manifestPath
            }
            classification = 'LOCAL_MACHINE_ONLY'
        }
        Assert-RelayLoopbackAddress -Address ([string]$machine.controlAddress)
        foreach ($port in @([int]$machine.controlPort, [int]$machine.runtimePort)) {
            if ($port -lt 1 -or $port -gt 65535) {
                throw 'Portable manifest contains an invalid control or runtime port.'
            }
        }
        Write-RelayJsonFile -Path $configFull -Value $machine | Out-Null
        [IO.Directory]::CreateDirectory($receiptRoot) | Out-Null
        Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $receiptRoot 'manifest.json')
        $receipt = [ordered]@{
            bundleId = [string]$manifest.bundleId
            archiveSha256 = Get-RelayFileSha256 -Path $archiveFull
            importedAtUtc = [DateTime]::UtcNow.ToString('o')
            fileCount = $planned.Count
            rootsRediscovered = $true
            secondMachineValidation = 'NOT_RUN'
        }
        Write-RelayJsonFile -Path (Join-Path $receiptRoot 'receipt.json') -Value $receipt | Out-Null
        return [ordered]@{
            status = 'imported'
            bundleId = [string]$manifest.bundleId
            configPath = $configFull
            fileCount = $planned.Count
            copiedFiles = @($planned | Where-Object { $_.action -eq 'copy_new' }).Count
            reusedMatchingFiles = @($planned | Where-Object { $_.action -eq 'reused_matching' }).Count
            rootsRediscovered = $true
            secondMachineValidated = $false
        }
    }
    finally {
        if (Test-Path -LiteralPath $extractRoot) {
            if (-not (Test-RelayPathContained -Parent $newData -Candidate $extractRoot)) {
                throw 'Refusing to clean an extraction path outside the new data root.'
            }
            Remove-Item -LiteralPath $extractRoot -Recurse -Force
        }
    }
}

try {
    $result = if ($Mode -eq 'Export') { Invoke-PortableExport } else { Invoke-PortableImport }
    Write-RelayResult -Value $result
    exit 0
}
catch {
    Write-RelayResult -Value ([ordered]@{
        status = 'failed'
        mode = $Mode
        error = [ordered]@{ code = 'PORTABLE_FAILED'; message = $_.Exception.Message }
        secondMachineValidated = $false
    })
    exit 2
}
