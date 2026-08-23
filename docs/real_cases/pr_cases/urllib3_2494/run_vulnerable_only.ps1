param(
    [int]$DurationSec = 600,
    [int]$DiagnosisTimeoutSec = 600,
    [string]$OutputRoot = ""
)

$runnerRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $runnerRoot "run_vulnerable_only.ps1") `
    -Case "urllib3_2494" `
    -DurationSec $DurationSec `
    -DiagnosisTimeoutSec $DiagnosisTimeoutSec `
    -OutputRoot $OutputRoot
exit $LASTEXITCODE
