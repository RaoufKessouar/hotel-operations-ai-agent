param(
    [switch]$LocalRules,
    [switch]$LocalQwen
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$projectEnv = Join-Path $projectRoot ".env"
$infraEnv = Join-Path $projectRoot "infra\qloapps\.env"

if (Test-Path -LiteralPath $projectEnv) {
    Get-Content -LiteralPath $projectEnv | ForEach-Object {
        if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }
}

if (Test-Path -LiteralPath $infraEnv) {
    Get-Content -LiteralPath $infraEnv | ForEach-Object {
        if ($_ -match '^(QLOAPPS_API_KEY|QLOAPPS_WRITE_API_KEY)=(.+)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }
    $env:AGENTPMS_QLOAPPS_API_KEY = $env:QLOAPPS_API_KEY
    $env:AGENTPMS_QLOAPPS_WRITE_API_KEY = $env:QLOAPPS_WRITE_API_KEY
}

if ($LocalRules) {
    $env:AGENTPMS_DECISION_PROVIDER = "local_rules"
} elseif ($LocalQwen) {
    $env:AGENTPMS_DECISION_PROVIDER = "openai_compatible"
    $env:AGENTPMS_LLM_BASE_URL = "http://127.0.0.1:8000/v1"
    $env:AGENTPMS_LLM_API_KEY = "EMPTY"
    $env:AGENTPMS_LLM_MODEL = "Qwen/Qwen2.5-14B-Instruct"
    $env:AGENTPMS_KNOWLEDGE_ENABLED = "false"

    try {
        $currentHealth = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3
        if ($currentHealth.status -ne "ready" -or `
            $currentHealth.model -ne "Qwen/Qwen2.5-14B-Instruct") {
            throw "Le service local 8000 n'est pas le Qwen attendu."
        }
    } catch {
        throw "Le serveur Qwen local doit etre disponible sur http://127.0.0.1:8000 avant d'utiliser -LocalQwen. Consultez infra/llm/README.md."
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" `
                -TimeoutSec 3
            if ($health.status -eq "ready" -and `
                $health.model -eq "Qwen/Qwen2.5-14B-Instruct") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $ready) {
        throw "Qwen ne repond pas apres le delai de demarrage."
    }
} else {
    if (-not $env:AGENTPMS_DECISION_PROVIDER) {
        $env:AGENTPMS_DECISION_PROVIDER = "deepseek"
    }
    if (-not $env:AGENTPMS_LLM_BASE_URL) {
        $env:AGENTPMS_LLM_BASE_URL = "https://api.deepseek.com/beta"
    }
    if (-not $env:AGENTPMS_LLM_MODEL) {
        $env:AGENTPMS_LLM_MODEL = "deepseek-v4-flash"
    }
}
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
Set-Location -LiteralPath $projectRoot
& $python -m uvicorn agentpms.api.main:app --host 127.0.0.1 --port 8090
