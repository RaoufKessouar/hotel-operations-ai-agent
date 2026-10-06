param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "portable_backups\qloapps")
)

$ErrorActionPreference = "Stop"
$container = "agentpms-qloapps"
$running = docker inspect -f '{{.State.Running}}' $container 2>$null
if ($LASTEXITCODE -ne 0 -or $running -ne "true") {
    throw "QloApps doit etre demarre avant la sauvegarde."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $OutputRoot $timestamp
New-Item -ItemType Directory -Force -Path $backup | Out-Null
$databaseDump = Join-Path $backup "qloapps-database.sql"
$applicationArchive = Join-Path $backup "qloapps-application.tar.gz"
$remoteArchive = "/tmp/agentpms-qloapps-$timestamp.tar.gz"

function Invoke-DockerToFile([string[]]$Arguments, [string]$Destination) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = "docker.exe"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Impossible de lancer Docker."
    }
    $stream = [IO.File]::Create($Destination)
    try {
        $process.StandardOutput.BaseStream.CopyTo($stream)
    } finally {
        $stream.Dispose()
    }
    $errors = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "Echec de la sauvegarde Docker : $errors"
    }
}

Invoke-DockerToFile @(
    "exec", $container, "sh", "-lc",
    'exec mysqldump --single-transaction --routines --triggers -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
) $databaseDump

try {
    docker exec $container tar -czf $remoteArchive `
        -C /home/qloapps/www/QloApps .
    if ($LASTEXITCODE -ne 0) {
        throw "Impossible d'archiver l'application QloApps."
    }
    docker cp "${container}:$remoteArchive" $applicationArchive
    if ($LASTEXITCODE -ne 0) {
        throw "Impossible de copier l'archive QloApps."
    }
} finally {
    docker exec $container rm -f $remoteArchive 2>$null | Out-Null
}

$configuration = Join-Path $backup "configuration"
New-Item -ItemType Directory -Force -Path $configuration | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "infra\qloapps") `
    -Destination $configuration -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env") `
        -Destination (Join-Path $configuration "agentpms.env") -Force
}

$manifest = Get-ChildItem -LiteralPath $backup -Recurse -File | ForEach-Object {
    [pscustomobject]@{
        path = $_.FullName.Substring($backup.Length + 1)
        bytes = $_.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
    }
}
[IO.File]::WriteAllText(
    (Join-Path $backup "manifest.json"),
    ($manifest | ConvertTo-Json -Depth 4),
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Sauvegarde QloApps terminee : $backup"
