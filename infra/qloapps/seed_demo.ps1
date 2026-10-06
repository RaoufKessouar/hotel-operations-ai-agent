$ErrorActionPreference = 'Stop'

$settings = @{}
Get-Content -LiteralPath (Join-Path $PSScriptRoot '.env') | ForEach-Object {
    if ($_ -match '^([^#=]+)=(.*)$') {
        $settings[$matches[1]] = $matches[2]
    }
}

$python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Environnement Python AGENTpms introuvable: $python"
}

docker compose --env-file (Join-Path $PSScriptRoot '.env') `
    -f (Join-Path $PSScriptRoot 'compose.yaml') up -d
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$databaseReady = $false
$deadline = (Get-Date).AddSeconds(45)
do {
    docker exec agentpms-qloapps mysqladmin ping -h 127.0.0.1 -uroot `
        "--password=$($settings['QLOAPPS_DB_ROOT_PASSWORD'])" --silent `
        2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $databaseReady = $true
        break
    }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if (-not $databaseReady) {
    throw 'La base QloApps ne repond pas apres 45 secondes.'
}

docker exec agentpms-qloapps php /opt/agentpms/bootstrap_seed_api.php
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
docker exec agentpms-qloapps php /opt/agentpms/configure_demo_catalog.php
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:QLOAPPS_BASE_URL = 'http://127.0.0.1:8088/api'
$env:QLOAPPS_SEED_API_KEY = $settings['QLOAPPS_SEED_API_KEY']
$seedExit = 0
try {
    & $python (Join-Path $PSScriptRoot 'seed_demo.py')
    $seedExit = $LASTEXITCODE
} finally {
    docker exec agentpms-qloapps php /opt/agentpms/disable_seed_api.php
    if ($LASTEXITCODE -ne 0 -and $seedExit -eq 0) {
        $seedExit = $LASTEXITCODE
    }
}

exit $seedExit
