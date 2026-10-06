param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDirectory,
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmRestore) {
    throw "Relancez avec -ConfirmRestore pour autoriser la restauration."
}
$backup = (Resolve-Path -LiteralPath $BackupDirectory).Path
$databaseDump = Join-Path $backup "qloapps-database.sql"
$applicationArchive = Join-Path $backup "qloapps-application.tar.gz"
foreach ($required in $databaseDump, $applicationArchive) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Fichier de sauvegarde absent : $required"
    }
}

$infra = Join-Path $PSScriptRoot "infra\qloapps"
$infraEnv = Join-Path $infra ".env"
$savedEnv = Join-Path $backup "configuration\qloapps\.env"
if (-not (Test-Path -LiteralPath $infraEnv)) {
    Copy-Item -LiteralPath $savedEnv -Destination $infraEnv -Force
}

Push-Location $infra
try {
    docker compose --env-file .env up -d
    if ($LASTEXITCODE -ne 0) {
        throw "QloApps n'a pas pu demarrer."
    }
} finally {
    Pop-Location
}

$container = "agentpms-qloapps"
$remoteArchive = "/tmp/agentpms-qloapps-restore.tar.gz"
docker cp $applicationArchive "${container}:$remoteArchive"
if ($LASTEXITCODE -ne 0) {
    throw "Impossible de copier l'application sauvegardee."
}
try {
    docker exec $container tar -xzf $remoteArchive `
        -C /home/qloapps/www/QloApps
    if ($LASTEXITCODE -ne 0) {
        throw "Impossible de restaurer l'application QloApps."
    }
} finally {
    docker exec $container rm -f $remoteArchive 2>$null | Out-Null
}

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = "docker.exe"
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardError = $true
foreach ($argument in @(
    "exec", "-i", $container, "sh", "-lc",
    'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
)) {
    $startInfo.ArgumentList.Add($argument)
}
$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) {
    throw "Impossible de lancer Docker."
}
$source = [IO.File]::OpenRead($databaseDump)
try {
    $source.CopyTo($process.StandardInput.BaseStream)
    $process.StandardInput.Close()
} finally {
    $source.Dispose()
}
$errors = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($process.ExitCode -ne 0) {
    throw "Echec de la restauration MySQL : $errors"
}

docker exec $container php /opt/agentpms/bootstrap_read_api.php
docker exec $container php /opt/agentpms/bootstrap_write_api.php
Write-Host "QloApps restaure depuis : $backup"
