[CmdletBinding()]
param(
    [string]$EnvFile = "gateway_runtime.env.local",
    [string]$CasesFile = "examples/search_harness_cases.jsonl",
    [int]$Concurrency = 8,
    [int]$Iterations = 20,
    [int]$WarmupIterations = 2,
    [string]$Prefix = "UMP_",
    [string]$ArtifactsDir = "artifacts/gateway_harness"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Set-EnvironmentFromFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Environment file not found: $Path"
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) {
            throw "Invalid environment entry: $line"
        }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        Set-Item -Path "Env:$name" -Value $value
    }
}

Set-EnvironmentFromFile -Path $EnvFile

$requiredVars = @(
    "${Prefix}AZURE_SEARCH_ENDPOINT",
    "${Prefix}ELASTICSEARCH_ENDPOINT"
)

foreach ($name in $requiredVars) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing required environment variable: $name"
    }
}

New-Item -ItemType Directory -Path $ArtifactsDir -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputPath = Join-Path $ArtifactsDir "gateway-harness-$timestamp.json"

Write-Host "Running gateway harness..."
Write-Host "  Env file:   $EnvFile"
Write-Host "  Cases file: $CasesFile"
Write-Host "  Output:     $outputPath"

python -m unified_modernization.gateway.harness `
    --cases-file $CasesFile `
    --concurrency $Concurrency `
    --iterations $Iterations `
    --warmup-iterations $WarmupIterations `
    --prefix $Prefix `
    --output $outputPath

if ($LASTEXITCODE -ne 0) {
    throw "Gateway harness failed with exit code $LASTEXITCODE"
}

Write-Host "Harness report written to $outputPath"
