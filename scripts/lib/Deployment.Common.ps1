Set-StrictMode -Version Latest

$script:RelayMachineConfigKind = 'relay-liveloop-machine'
$script:RelayMachineConfigVersion = 1

function ConvertTo-RelayJson {
    param([Parameter(Mandatory = $true)]$Value)

    return ($Value | ConvertTo-Json -Depth 32 -Compress)
}

function Write-RelayResult {
    param([Parameter(Mandatory = $true)]$Value)

    [Console]::Out.WriteLine((ConvertTo-RelayJson -Value $Value))
}

function Get-RelayFullPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$BasePath = (Get-Location).Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'A required path is empty.'
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    if (-not [IO.Path]::IsPathRooted($expanded)) {
        $expanded = [IO.Path]::Combine($BasePath, $expanded)
    }
    return [IO.Path]::GetFullPath($expanded)
}

function Get-RelayExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet('Any', 'File', 'Directory')][string]$Kind = 'Any'
    )

    $full = Get-RelayFullPath -Path $Path
    $pathKind = switch ($Kind) {
        'File' { 'Leaf' }
        'Directory' { 'Container' }
        default { 'Any' }
    }
    if ($pathKind -eq 'Any') {
        if (-not (Test-Path -LiteralPath $full)) {
            throw "Required path does not exist: $full"
        }
    }
    elseif (-not (Test-Path -LiteralPath $full -PathType $pathKind)) {
        throw "Required $($Kind.ToLowerInvariant()) does not exist: $full"
    }
    return (Get-Item -LiteralPath $full -Force).FullName
}

function Test-RelayPathContained {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Candidate
    )

    $parentFull = (Get-RelayFullPath -Path $Parent).TrimEnd('\', '/')
    $candidateFull = Get-RelayFullPath -Path $Candidate
    if ([string]::Equals($parentFull, $candidateFull.TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $parentFull + [IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-RelayPathOutsideRoots {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Roots,
        [string]$Label = 'Path'
    )

    foreach ($root in $Roots) {
        if (-not [string]::IsNullOrWhiteSpace($root) -and (Test-RelayPathContained -Parent $root -Candidate $Path)) {
            throw "$Label must be outside repository roots: $Path"
        }
    }
}

function Assert-RelayPathOutsideGitTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = 'Path'
    )

    $full = Get-RelayFullPath -Path $Path
    $current = if (Test-Path -LiteralPath $full -PathType Container) {
        [IO.DirectoryInfo]$full
    }
    else {
        [IO.DirectoryInfo](Split-Path -Parent $full)
    }
    while ($null -ne $current) {
        if (Test-Path -LiteralPath (Join-Path $current.FullName '.git')) {
            throw "$Label must be outside a Git working tree: $full"
        }
        $current = $current.Parent
    }
}

function Read-RelayJsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Get-RelayExistingPath -Path $Path -Kind File
    try {
        return (Get-Content -LiteralPath $full -Raw -Encoding UTF8 | ConvertFrom-Json)
    }
    catch {
        throw "JSON file is invalid: $full"
    }
}

function Write-RelayJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value,
        [switch]$AllowReplace
    )

    $full = Get-RelayFullPath -Path $Path
    $parent = Split-Path -Parent $full
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    if ((Test-Path -LiteralPath $full) -and -not $AllowReplace) {
        throw "Refusing to overwrite existing file: $full"
    }
    $temporary = Join-Path $parent ('.relay-write-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = ($Value | ConvertTo-Json -Depth 32)
        [IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        if ($AllowReplace -and (Test-Path -LiteralPath $full)) {
            $backup = Join-Path $parent ('.relay-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            try {
                [IO.File]::Replace($temporary, $full, $backup)
            }
            finally {
                if (Test-Path -LiteralPath $backup) {
                    Remove-Item -LiteralPath $backup -Force
                }
            }
        }
        else {
            Move-Item -LiteralPath $temporary -Destination $full
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    return $full
}

function Get-RelayRequiredProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value -or ($property.Value -is [string] -and [string]::IsNullOrWhiteSpace($property.Value))) {
        throw "Machine configuration is missing required field '$Name'."
    }
    return $property.Value
}

function Assert-RelayLoopbackAddress {
    param([Parameter(Mandatory = $true)][string]$Address)

    if ($Address -notin @('127.0.0.1', 'localhost', '::1')) {
        throw 'controlAddress must be a loopback address.'
    }
}

function Get-RelayMachineConfig {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    $config = Read-RelayJsonFile -Path $ConfigPath
    if ((Get-RelayRequiredProperty -Object $config -Name 'configKind') -ne $script:RelayMachineConfigKind) {
        throw 'Machine configuration has an unsupported configKind.'
    }
    if ([int](Get-RelayRequiredProperty -Object $config -Name 'schemaVersion') -ne $script:RelayMachineConfigVersion) {
        throw 'Machine configuration has an unsupported schemaVersion.'
    }
    foreach ($name in @('toolRepoRoot', 'gameRepoRoot', 'gameProjectRoot', 'dataRoot', 'pythonExecutable', 'unityExecutable', 'tokenFile', 'controlAddress', 'controlPort', 'runtimePort', 'lifecycle')) {
        Get-RelayRequiredProperty -Object $config -Name $name | Out-Null
    }
    Assert-RelayLoopbackAddress -Address ([string]$config.controlAddress)
    foreach ($portName in @('controlPort', 'runtimePort')) {
        $port = [int]$config.$portName
        if ($port -lt 1 -or $port -gt 65535) {
            throw "$portName must be in 1..65535."
        }
    }
    return $config
}

function Assert-RelayMachineConfigPaths {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [switch]$RequireUnity
    )

    $toolRoot = Get-RelayExistingPath -Path ([string]$Config.toolRepoRoot) -Kind Directory
    $gameRepo = Get-RelayExistingPath -Path ([string]$Config.gameRepoRoot) -Kind Directory
    $gameProject = Get-RelayExistingPath -Path ([string]$Config.gameProjectRoot) -Kind Directory
    $dataRoot = Get-RelayExistingPath -Path ([string]$Config.dataRoot) -Kind Directory
    Get-RelayExistingPath -Path ([string]$Config.pythonExecutable) -Kind File | Out-Null
    if ($RequireUnity) {
        Get-RelayExistingPath -Path ([string]$Config.unityExecutable) -Kind File | Out-Null
    }
    Get-RelayExistingPath -Path ([string]$Config.tokenFile) -Kind File | Out-Null
    if (-not (Test-RelayPathContained -Parent $gameRepo -Candidate $gameProject)) {
        throw 'Configured game project is outside the configured game repository.'
    }
    Assert-RelayPathOutsideRoots -Path $dataRoot -Roots @($toolRoot, $gameRepo) -Label 'Configured data root'
    Assert-RelayPathOutsideRoots -Path (Get-RelayFullPath -Path $ConfigPath) -Roots @($toolRoot, $gameRepo) -Label 'Machine configuration'
    Assert-RelayPathOutsideRoots -Path ([string]$Config.tokenFile) -Roots @($toolRoot, $gameRepo) -Label 'Token file'
    Assert-RelayPathOutsideGitTree -Path $dataRoot -Label 'Configured data root'
    Assert-RelayPathOutsideGitTree -Path (Get-RelayFullPath -Path $ConfigPath) -Label 'Machine configuration'
    Assert-RelayPathOutsideGitTree -Path ([string]$Config.tokenFile) -Label 'Token file'
    $lifecycle = $Config.lifecycle
    foreach ($path in @(
        [string](Get-RelayRequiredProperty -Object $lifecycle -Name 'stateRoot'),
        [string](Get-RelayRequiredProperty -Object $lifecycle -Name 'databasePath')
    )) {
        if (-not (Test-RelayPathContained -Parent $dataRoot -Candidate $path)) {
            throw 'Configured lifecycle path escapes dataRoot.'
        }
    }
    $artifactProperty = $lifecycle.PSObject.Properties['artifactRoots']
    if ($null -eq $artifactProperty -or @($artifactProperty.Value).Count -eq 0) {
        throw 'Machine configuration requires at least one lifecycle artifact root.'
    }
    foreach ($artifactRoot in @($artifactProperty.Value)) {
        if (-not (Test-RelayPathContained -Parent $dataRoot -Candidate ([string]$artifactRoot))) {
            throw 'Configured artifact root escapes dataRoot.'
        }
    }
}

function Get-RelayProjectVersion {
    param([Parameter(Mandatory = $true)][string]$GameProjectRoot)

    $versionFile = Join-Path $GameProjectRoot 'ProjectSettings\ProjectVersion.txt'
    $versionFile = Get-RelayExistingPath -Path $versionFile -Kind File
    $match = Select-String -LiteralPath $versionFile -Pattern '^m_EditorVersion:\s*(?<version>\S+)' | Select-Object -First 1
    if ($null -eq $match) {
        throw "Unity project version is missing from: $versionFile"
    }
    return $match.Matches[0].Groups['version'].Value
}

function Resolve-RelayPythonExecutable {
    param([string]$ConfiguredPath)

    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        $candidates.Add((Get-RelayFullPath -Path $ConfiguredPath))
    }
    else {
        foreach ($command in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $candidates.Add($command.Source)
        }
        foreach ($pattern in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'),
            (Join-Path $env:ProgramFiles 'Python*\python.exe')
        )) {
            foreach ($item in @(Get-Item -Path $pattern -ErrorAction SilentlyContinue)) {
                $candidates.Add($item.FullName)
            }
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $output = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and (($output | Out-String) -match '^Python\s+3\.')) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    throw 'A working installed Python 3 executable was not found. No package was installed or upgraded.'
}

function Resolve-RelayUnityExecutable {
    param(
        [string]$ConfiguredPath,
        [Parameter(Mandatory = $true)][string]$RequiredVersion
    )

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        $full = Get-RelayExistingPath -Path $ConfiguredPath -Kind File
        if ([IO.Path]::GetExtension($full) -ne '.exe') {
            throw 'Configured Unity executable must be an existing .exe file.'
        }
        return $full
    }
    $candidates = [Collections.Generic.List[string]]::new()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ([string]::IsNullOrWhiteSpace($base)) {
            continue
        }
        $candidates.Add((Join-Path $base "Unity\Hub\Editor\$RequiredVersion\Editor\Unity.exe"))
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    throw "Unity $RequiredVersion was not found in configured or standard Unity Hub locations. No Unity package was installed or upgraded."
}

function New-RelayTokenFileIfMissing {
    param([Parameter(Mandatory = $true)][string]$TokenFile)

    $full = Get-RelayFullPath -Path $TokenFile
    if (Test-Path -LiteralPath $full) {
        $item = Get-Item -LiteralPath $full -Force
        if ($item.PSIsContainer -or $item.Length -gt 4096) {
            throw 'Existing token file is not a regular token file no larger than 4096 bytes.'
        }
        $existing = (Get-Content -LiteralPath $full -Raw -Encoding UTF8).Trim()
        if ([string]::IsNullOrWhiteSpace($existing)) {
            throw 'Existing token file is empty; refusing to overwrite it.'
        }
        return @{ path = $item.FullName; created = $false }
    }

    $parent = Split-Path -Parent $full
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes)
    $temporary = Join-Path $parent ('.relay-token-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $token + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $full
        try {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $acl = New-Object Security.AccessControl.FileSecurity
            $acl.SetAccessRuleProtection($true, $false)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'Modify', 'Allow')
            $acl.AddAccessRule($rule)
            Set-Acl -LiteralPath $full -AclObject $acl
        }
        catch {
            Remove-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
            throw 'Token file was created but its access could not be restricted; the new file was removed.'
        }
    }
    finally {
        $token = $null
        [Array]::Clear($bytes, 0, $bytes.Length)
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    return @{ path = (Get-Item -LiteralPath $full).FullName; created = $true }
}

function Read-RelayToken {
    param([Parameter(Mandatory = $true)][string]$TokenFile)

    $full = Get-RelayExistingPath -Path $TokenFile -Kind File
    $item = Get-Item -LiteralPath $full
    if ($item.Length -gt 4096) {
        throw 'Configured token file exceeds 4096 bytes.'
    }
    $token = (Get-Content -LiteralPath $full -Raw -Encoding UTF8).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'Configured token file is empty.'
    }
    return $token
}

function Get-RelayControlUrl {
    param([Parameter(Mandatory = $true)]$Config)

    $address = [string]$Config.controlAddress
    Assert-RelayLoopbackAddress -Address $address
    if ($address -eq '::1') {
        $address = '[::1]'
    }
    return "http://${address}:$([int]$Config.controlPort)"
}

function Get-RelayHostProbe {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [ValidateSet('status', 'capabilities')][string]$Endpoint = 'status',
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 3
    )

    try {
        $token = Read-RelayToken -TokenFile ([string]$Config.tokenFile)
        $headers = @{ Authorization = 'Bearer ' + $token; Accept = 'application/json' }
        $body = Invoke-RestMethod -Method Get -Uri ((Get-RelayControlUrl -Config $Config) + '/' + $Endpoint) -Headers $headers -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        $valid = $false
        if ($Endpoint -eq 'status') {
            $valid = $body.service -eq 'Relay LiveLoop' -and [int]$body.protocolVersion -eq 1
        }
        else {
            $valid = [int]$body.protocolVersion -eq 1 -and $null -ne $body.capabilities
        }
        return [pscustomobject]@{ reachable = $true; valid = $valid; body = $body; error = $null }
    }
    catch {
        return [pscustomobject]@{ reachable = $false; valid = $false; body = $null; error = $_.Exception.GetType().Name }
    }
    finally {
        $token = $null
        $headers = $null
    }
}

function Get-RelayHostLifecycleView {
    param([Parameter(Mandatory = $true)]$StatusBody)

    $property = $StatusBody.PSObject.Properties['hostLifecycle']
    if ($null -eq $property -or $null -eq $property.Value) {
        return [pscustomobject]@{ available = $false; valid = $false; lifecycle = $null; reason = 'hostLifecycle_missing' }
    }
    $lifecycle = $property.Value
    foreach ($name in @('state', 'acceptingCommands', 'shutdownRequested', 'safeToExit', 'playerPolicy', 'activeJobs', 'nonInterruptibleJobs')) {
        if ($null -eq $lifecycle.PSObject.Properties[$name]) {
            return [pscustomobject]@{ available = $true; valid = $false; lifecycle = $lifecycle; reason = "hostLifecycle_$($name)_missing" }
        }
    }
    if ($lifecycle.state -notin @('running', 'draining') -or $lifecycle.playerPolicy -ne 'preserve') {
        return [pscustomobject]@{ available = $true; valid = $false; lifecycle = $lifecycle; reason = 'hostLifecycle_value_invalid' }
    }
    if ($lifecycle.acceptingCommands -isnot [bool] -or $lifecycle.shutdownRequested -isnot [bool] -or $lifecycle.safeToExit -isnot [bool]) {
        return [pscustomobject]@{ available = $true; valid = $false; lifecycle = $lifecycle; reason = 'hostLifecycle_boolean_invalid' }
    }
    return [pscustomobject]@{ available = $true; valid = $true; lifecycle = $lifecycle; reason = $null }
}

function Invoke-RelayHostShutdown {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [ValidateRange(1, 30)][int]$TimeoutSeconds = 5
    )

    $requestId = 'shutdown_' + [Guid]::NewGuid().ToString('N')
    $request = [ordered]@{
        protocolVersion = 1
        requestId = $requestId
        mode = 'graceful'
        preservePlayer = $true
        waitForActiveJobs = $true
    }
    try {
        $token = Read-RelayToken -TokenFile ([string]$Config.tokenFile)
        $headers = @{ Authorization = 'Bearer ' + $token; Accept = 'application/json' }
        $body = Invoke-RestMethod -Method Post -Uri ((Get-RelayControlUrl -Config $Config) + '/lifecycle/shutdown') -Headers $headers -ContentType 'application/json' -Body (ConvertTo-RelayJson -Value $request) -TimeoutSec $TimeoutSeconds -ErrorAction Stop
        $accepted = $body.status -eq 'accepted' -and $body.result.shutdownAccepted -eq $true
        return [pscustomobject]@{ reachable = $true; accepted = $accepted; requestId = $requestId; body = $body; httpStatus = 202; error = $null }
    }
    catch {
        $failure = $_
        $errorBody = $null
        if ($null -ne $failure.ErrorDetails -and -not [string]::IsNullOrWhiteSpace($failure.ErrorDetails.Message)) {
            try { $errorBody = $failure.ErrorDetails.Message | ConvertFrom-Json } catch { $errorBody = $null }
        }
        $statusCode = $null
        try { $statusCode = [int]$failure.Exception.Response.StatusCode } catch { $statusCode = $null }
        return [pscustomobject]@{
            reachable = $null -ne $statusCode
            accepted = $false
            requestId = $requestId
            body = $errorBody
            httpStatus = $statusCode
            error = $failure.Exception.GetType().Name
        }
    }
    finally {
        $token = $null
        $headers = $null
    }
}

function ConvertTo-RelayNativeArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
        }
        else {
            if ($slashes -gt 0) {
                [void]$builder.Append(('\' * $slashes))
            }
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    if ($slashes -gt 0) {
        [void]$builder.Append(('\' * ($slashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Start-RelayHiddenProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $argumentLine = (($Arguments | ForEach-Object { ConvertTo-RelayNativeArgument -Value ([string]$_) }) -join ' ')
    return Start-Process -FilePath $Executable -ArgumentList $argumentLine -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
}

function Get-RelayProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    try {
        $path = $process.Path
        $startTime = $process.StartTime.ToUniversalTime().ToString('o')
    }
    catch {
        return $null
    }
    $commandLine = $null
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        $commandLine = $cim.CommandLine
        if (-not [string]::IsNullOrWhiteSpace($cim.ExecutablePath)) {
            $path = $cim.ExecutablePath
        }
    }
    catch {
        $commandLine = $null
    }
    return [pscustomobject]@{
        processId = $ProcessId
        startTimeUtc = $startTime
        executablePath = if ($path) { Get-RelayFullPath -Path $path } else { $null }
        commandLine = $commandLine
    }
}

function ConvertTo-RelayUtcTimestamp {
    param([Parameter(Mandatory = $true)]$Value)

    if ($Value -is [DateTime]) {
        return ([DateTime]$Value).ToUniversalTime().ToString('o')
    }
    if ($Value -is [DateTimeOffset]) {
        return ([DateTimeOffset]$Value).UtcDateTime.ToString('o')
    }
    $parsed = [DateTimeOffset]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
    return $parsed.UtcDateTime.ToString('o')
}

function New-RelayProcessRecord {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][bool]$Owned,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [string]$ProjectPath,
        [string[]]$ArgumentAnchors = @()
    )

    $snapshot = Get-RelayProcessSnapshot -ProcessId $Process.Id
    if ($null -eq $snapshot) {
        throw "$Role process exited before its identity could be recorded."
    }
    return [ordered]@{
        recordVersion = 1
        role = $Role
        processId = $snapshot.processId
        startTimeUtc = $snapshot.startTimeUtc
        executablePath = $snapshot.executablePath
        ownedByDeployment = $Owned
        ownershipId = if ($Owned) { 'owned_' + [Guid]::NewGuid().ToString('N') } else { $null }
        configPath = Get-RelayFullPath -Path $ConfigPath
        projectPath = if ($ProjectPath) { Get-RelayFullPath -Path $ProjectPath } else { $null }
        argumentAnchors = @($ArgumentAnchors)
        recordedAtUtc = [DateTime]::UtcNow.ToString('o')
    }
}

function Test-RelayProcessIdentity {
    param([Parameter(Mandatory = $true)]$Record)

    foreach ($name in @('processId', 'startTimeUtc', 'executablePath', 'ownedByDeployment')) {
        if ($null -eq $Record.PSObject.Properties[$name]) {
            return [pscustomobject]@{ matched = $false; reason = 'record_incomplete'; snapshot = $null }
        }
    }
    $snapshot = Get-RelayProcessSnapshot -ProcessId ([int]$Record.processId)
    if ($null -eq $snapshot) {
        return [pscustomobject]@{ matched = $false; reason = 'process_not_running'; snapshot = $null }
    }
    $recordedStart = ConvertTo-RelayUtcTimestamp -Value $Record.startTimeUtc
    $actualStart = ConvertTo-RelayUtcTimestamp -Value $snapshot.startTimeUtc
    if (-not [string]::Equals($recordedStart, $actualStart, [StringComparison]::Ordinal)) {
        return [pscustomobject]@{ matched = $false; reason = 'start_time_mismatch'; snapshot = $snapshot }
    }
    $expectedPath = Get-RelayFullPath -Path ([string]$Record.executablePath)
    if (-not [string]::Equals($expectedPath, [string]$snapshot.executablePath, [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ matched = $false; reason = 'executable_path_mismatch'; snapshot = $snapshot }
    }
    foreach ($anchor in @($Record.argumentAnchors)) {
        if ([string]::IsNullOrWhiteSpace($snapshot.commandLine) -or $snapshot.commandLine.IndexOf([string]$anchor, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return [pscustomobject]@{ matched = $false; reason = 'command_line_anchor_mismatch'; snapshot = $snapshot }
        }
    }
    return [pscustomobject]@{ matched = $true; reason = $null; snapshot = $snapshot }
}

function Get-RelayStateRoot {
    param([Parameter(Mandatory = $true)]$Config)

    $lifecycle = Get-RelayRequiredProperty -Object $Config -Name 'lifecycle'
    return Get-RelayFullPath -Path ([string](Get-RelayRequiredProperty -Object $lifecycle -Name 'stateRoot'))
}

function Get-RelayProcessRecordPath {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [ValidateSet('host', 'editor')][string]$Role
    )

    return Join-Path (Get-RelayStateRoot -Config $Config) ($Role + '.json')
}

function Get-RelayFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath (Get-RelayExistingPath -Path $Path -Kind File) -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-RelayReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    return (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}
