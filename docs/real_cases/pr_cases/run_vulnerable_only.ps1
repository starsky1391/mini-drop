param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("playwright_3004", "vllm_38602", "aiohttp_10570", "kafka_2286", "pyav_751")]
    [string]$Case,

    [int]$DurationSec = 600,
    [int]$DiagnosisTimeoutSec = 600,
    [string]$OutputRoot = ""
)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $repoRoot "reports/eval/real-open-source/pr-cases"
}

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$outputDir = Join-Path $OutputRoot "$Case-vulnerable-600s-$ts"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

python (Join-Path $PSScriptRoot "run_pr_case_vm.py") `
    --case $Case `
    --mode vulnerable `
    --duration-sec $DurationSec `
    --diagnosis-timeout-sec $DiagnosisTimeoutSec `
    --output-root $outputDir
