param(
    [int]$DurationSec = 400,
    [int]$DiagnosisTimeoutSec = 400,
    [string]$OutputRoot = "reports/eval/real-open-source"
)

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$outputDir = Join-Path $OutputRoot "celery-8882-vulnerable-400s-$ts"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

python (Join-Path $PSScriptRoot "run_case_vm.py") `
    --duration-sec $DurationSec `
    --diagnosis-timeout-sec $DiagnosisTimeoutSec `
    --output-json (Join-Path $outputDir "run.json")
