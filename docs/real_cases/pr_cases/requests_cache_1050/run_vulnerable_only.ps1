param(
    [int]$DurationSec = 400,
    [int]$DiagnosisTimeoutSec = 1200,
    [string]$OutputRoot = ""
)

$runnerRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $runnerRoot "run_vulnerable_only.ps1") `
    -Case "requests_cache_1050" `
    -DurationSec $DurationSec `
    -DiagnosisTimeoutSec $DiagnosisTimeoutSec `
    -OutputRoot $OutputRoot
exit $LASTEXITCODE
