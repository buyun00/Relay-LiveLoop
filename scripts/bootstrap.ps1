[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$ToolRepoRoot,
    [Parameter(Mandatory = $true)][string]$GameRepoRoot,
    [Parameter(Mandatory = $true)][string]$GameProjectRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$PythonExecutable,
    [string]$UnityExecutable,
    [string[]]$SdkRoot = @(),
    [string]$TokenFile,
    [string]$ProjectConfigFile,
    [string]$NativeCompileProfilesFile,
    [string]$EditorJobRoot,
    [string]$EditorArtifactRoot,
    [string]$RuntimeSessionFile,
    [ValidateSet('127.0.0.1', 'localhost', '::1')][string]$ControlAddress = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$ControlPort = 18760,
    [ValidateRange(1, 65535)][int]$RuntimePort = 18761
)

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'lib\Deployment.Common.ps1')

$createdTokenPath = $null
try {
    $configFull = Get-RelayFullPath -Path $ConfigPath
    if (Test-Path -LiteralPath $configFull) {
        $existing = Get-RelayMachineConfig -ConfigPath $configFull
        Assert-RelayMachineConfigPaths -Config $existing -ConfigPath $configFull -RequireUnity
        Write-RelayResult -Value ([ordered]@{
            status = 'already_configured'
            changed = $false
            configPath = $configFull
            configKind = $existing.configKind
            schemaVersion = $existing.schemaVersion
            tokenChanged = $false
        })
        exit 0
    }

    $toolRoot = Get-RelayExistingPath -Path $ToolRepoRoot -Kind Directory
    $gameRepo = Get-RelayExistingPath -Path $GameRepoRoot -Kind Directory
    $gameProject = Get-RelayExistingPath -Path $GameProjectRoot -Kind Directory
    if ((Test-RelayPathContained -Parent $toolRoot -Candidate $gameRepo) -or (Test-RelayPathContained -Parent $gameRepo -Candidate $toolRoot)) {
        throw 'Tool and game repository roots must not contain one another.'
    }
    if (-not (Test-RelayPathContained -Parent $gameRepo -Candidate $gameProject)) {
        throw 'gameProjectRoot must be contained by gameRepoRoot.'
    }
    foreach ($marker in @('Assets', 'Packages', 'ProjectSettings')) {
        Get-RelayExistingPath -Path (Join-Path $gameProject $marker) -Kind Directory | Out-Null
    }
    Get-RelayExistingPath -Path (Join-Path $toolRoot 'relay_liveloop.py') -Kind File | Out-Null

    $dataFull = Get-RelayFullPath -Path $DataRoot
    Assert-RelayPathOutsideRoots -Path $dataFull -Roots @($toolRoot, $gameRepo) -Label 'Data root'
    Assert-RelayPathOutsideGitTree -Path $dataFull -Label 'Data root'
    Assert-RelayPathOutsideRoots -Path $configFull -Roots @($toolRoot, $gameRepo) -Label 'Machine configuration'
    Assert-RelayPathOutsideGitTree -Path $configFull -Label 'Machine configuration'

    if ([string]::IsNullOrWhiteSpace($TokenFile)) {
        $TokenFile = Join-Path (Split-Path -Parent $configFull) 'relay-liveloop.token'
    }
    $tokenFull = Get-RelayFullPath -Path $TokenFile
    Assert-RelayPathOutsideRoots -Path $tokenFull -Roots @($toolRoot, $gameRepo) -Label 'Token file'
    Assert-RelayPathOutsideGitTree -Path $tokenFull -Label 'Token file'

    $projectVersion = Get-RelayProjectVersion -GameProjectRoot $gameProject
    $pythonFull = Resolve-RelayPythonExecutable -ConfiguredPath $PythonExecutable
    $unityFull = Resolve-RelayUnityExecutable -ConfiguredPath $UnityExecutable -RequiredVersion $projectVersion

    $sdkRoots = @()
    foreach ($root in $SdkRoot) {
        $sdkRoots += Get-RelayExistingPath -Path $root -Kind Directory
    }
    $projectConfigFull = $null
    if (-not [string]::IsNullOrWhiteSpace($ProjectConfigFile)) {
        $projectConfigFull = Get-RelayExistingPath -Path $ProjectConfigFile -Kind File
        if (-not (Test-RelayPathContained -Parent $gameRepo -Candidate $projectConfigFull)) {
            throw 'Project configuration must be stored in the game repository.'
        }
    }

    [IO.Directory]::CreateDirectory($dataFull) | Out-Null
    $stateRoot = Join-Path $dataFull 'deployment\process-state'
    $databasePath = Join-Path $dataFull 'host\relay-liveloop.sqlite3'
    $artifactRoot = Join-Path $dataFull 'artifacts'
    foreach ($directory in @($stateRoot, (Split-Path -Parent $databasePath), $artifactRoot)) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }

    $tokenResult = New-RelayTokenFileIfMissing -TokenFile $tokenFull
    if ($tokenResult.created) {
        $createdTokenPath = [string]$tokenResult.path
    }

    $config = [ordered]@{
        configKind = $script:RelayMachineConfigKind
        schemaVersion = $script:RelayMachineConfigVersion
        generatedAtUtc = [DateTime]::UtcNow.ToString('o')
        toolRepoRoot = $toolRoot
        gameRepoRoot = $gameRepo
        gameProjectRoot = $gameProject
        dataRoot = $dataFull
        unityExecutable = $unityFull
        unityProjectVersion = $projectVersion
        pythonExecutable = $pythonFull
        sdkRoots = @($sdkRoots)
        projectConfigFile = $projectConfigFull
        controlAddress = $ControlAddress
        controlPort = $ControlPort
        runtimePort = $RuntimePort
        tokenFile = [string]$tokenResult.path
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
            importedBundleId = $null
        }
        classification = 'LOCAL_MACHINE_ONLY'
    }
    $nativeValues = @($NativeCompileProfilesFile, $EditorJobRoot, $EditorArtifactRoot)
    $nativeValueCount = @($nativeValues | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
    if ($nativeValueCount -notin @(0, 3)) {
        throw 'Native compiler configuration requires all three profile/job/artifact paths.'
    }
    if ($nativeValueCount -eq 3) {
        $config.nativeCompile = [ordered]@{
            profilesFile = Get-RelayExistingPath -Path $NativeCompileProfilesFile -Kind File
            editorJobRoot = Get-RelayFullPath -Path $EditorJobRoot
            editorArtifactRoot = Get-RelayFullPath -Path $EditorArtifactRoot
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($RuntimeSessionFile)) {
        $config.runtimeSessionFile = Get-RelayExistingPath -Path $RuntimeSessionFile -Kind File
    }
    Assert-RelayNativeCompileConfig -Config ([pscustomobject]$config)
    # Validate before publishing the machine configuration; the temporary file is task owned.
    $validationFile = Join-Path $stateRoot ('profile-check-' + [Guid]::NewGuid().ToString('N') + '.json')
    try {
        Write-RelayJsonFile -Path $validationFile -Value $config | Out-Null
        if ($nativeValueCount -eq 3) {
            Get-RelayNativeCompileInspection -Config ([pscustomobject]$config) -ConfigPath $validationFile | Out-Null
        }
    }
    finally {
        if (Test-Path -LiteralPath $validationFile) { Remove-Item -LiteralPath $validationFile -Force }
    }
    Write-RelayJsonFile -Path $configFull -Value $config | Out-Null

    Write-RelayResult -Value ([ordered]@{
        status = 'configured'
        changed = $true
        configPath = $configFull
        pythonInstalled = $true
        unityInstalled = $true
        sdkRootsValidated = @($sdkRoots).Count
        tokenCreated = [bool]$tokenResult.created
        commercialPackagesInstalledOrUpgraded = $false
        interactiveDesktopVerified = $false
        nativeRuntimeVerified = $false
    })
    exit 0
}
catch {
    if ($createdTokenPath -and (Test-Path -LiteralPath $createdTokenPath -PathType Leaf)) {
        Remove-Item -LiteralPath $createdTokenPath -Force -ErrorAction SilentlyContinue
    }
    Write-RelayResult -Value ([ordered]@{
        status = 'failed'
        changed = $false
        error = [ordered]@{
            code = 'BOOTSTRAP_FAILED'
            message = $_.Exception.Message
        }
    })
    exit 2
}
