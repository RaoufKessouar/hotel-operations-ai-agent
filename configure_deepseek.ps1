$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$secureKey = Read-Host "Cle API DeepSeek" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw "La cle API DeepSeek ne peut pas etre vide."
    }
    $managedKeys = @(
        "DEEPSEEK_API_KEY",
        "AGENTPMS_DECISION_PROVIDER",
        "AGENTPMS_LLM_BASE_URL",
        "AGENTPMS_LLM_MODEL",
        "AGENTPMS_LLM_REASONING_EFFORT"
    )
    $existing = @()
    if (Test-Path -LiteralPath $envPath) {
        $existing = Get-Content -LiteralPath $envPath | Where-Object {
            $line = $_
            -not ($managedKeys | Where-Object { $line -match "^$([regex]::Escape($_))=" })
        }
    }
    $updated = @(
        $existing
        "DEEPSEEK_API_KEY=$plainKey"
        "AGENTPMS_DECISION_PROVIDER=deepseek"
        "AGENTPMS_LLM_BASE_URL=https://api.deepseek.com/beta"
        "AGENTPMS_LLM_MODEL=deepseek-v4-flash"
        "AGENTPMS_LLM_REASONING_EFFORT=low"
    )
    [IO.File]::WriteAllLines(
        $envPath,
        [string[]]$updated,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Host "DeepSeek est configure dans .env (fichier ignore par Git)."
} finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    $plainKey = $null
}
