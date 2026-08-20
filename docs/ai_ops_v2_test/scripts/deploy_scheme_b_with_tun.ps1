[CmdletBinding()]
param(
    [int]$TunSettleSeconds = 3,
    [switch]$SkipAiCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PipeName = "verge-mihomo"
$DeployScript = Join-Path $PSScriptRoot "deploy_scheme_b_vm.py"

function Invoke-MihomoRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Body = ""
    )

    $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(
        ".",
        $PipeName,
        [System.IO.Pipes.PipeDirection]::InOut
    )
    try {
        $pipe.Connect(5000)
        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
        $headers = @(
            "$Method $Path HTTP/1.1"
            "Host: localhost"
            "Content-Type: application/json"
            "Content-Length: $($bodyBytes.Length)"
            "Connection: close"
            ""
            ""
        ) -join "`r`n"
        $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($headers)
        $pipe.Write($headerBytes, 0, $headerBytes.Length)
        if ($bodyBytes.Length -gt 0) {
            $pipe.Write($bodyBytes, 0, $bodyBytes.Length)
        }
        $pipe.Flush()

        $reader = [System.IO.StreamReader]::new(
            $pipe,
            [System.Text.Encoding]::UTF8,
            $false,
            4096,
            $true
        )
        $response = $reader.ReadToEnd()
    }
    finally {
        $pipe.Dispose()
    }

    $parts = $response -split "`r?`n`r?`n", 2
    $statusLine = ($parts[0] -split "`r?`n")[0]
    if ($statusLine -notmatch '^HTTP/\d(?:\.\d)?\s+(\d{3})') {
        throw "Mihomo returned an invalid HTTP response"
    }
    $statusCode = [int]$Matches[1]
    if ($statusCode -lt 200 -or $statusCode -ge 300) {
        throw "Mihomo request failed: $statusLine"
    }
    return [pscustomobject]@{
        StatusCode = $statusCode
        Body = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    }
}

function Get-MihomoTunState {
    $response = Invoke-MihomoRequest -Method "GET" -Path "/configs"
    $config = $response.Body | ConvertFrom-Json
    return [bool]$config.tun.enable
}

function Set-MihomoTunState {
    param([Parameter(Mandatory = $true)][bool]$Enabled)

    $body = @{ tun = @{ enable = $Enabled } } | ConvertTo-Json -Compress
    $null = Invoke-MihomoRequest -Method "PATCH" -Path "/configs" -Body $body
    Start-Sleep -Seconds $TunSettleSeconds
    if ((Get-MihomoTunState) -ne $Enabled) {
        throw "Mihomo did not apply tun.enable=$Enabled"
    }
}

if (-not $env:MINI_DROP_VM_PASSWORD) {
    throw "MINI_DROP_VM_PASSWORD is required"
}

$originalTunState = Get-MihomoTunState
$tunChanged = $false
$deploySucceeded = $false

try {
    if (-not $originalTunState) {
        Write-Host "[clash] enabling TUN for VM image build"
        Set-MihomoTunState -Enabled $true
        $tunChanged = $true
    }
    else {
        Write-Host "[clash] TUN already enabled; preserving current state"
    }

    & python $DeployScript
    if ($LASTEXITCODE -ne 0) {
        throw "Scheme B deployment failed with exit code $LASTEXITCODE"
    }
    $deploySucceeded = $true
}
finally {
    if ($tunChanged) {
        Write-Host "[clash] restoring TUN to disabled"
        Set-MihomoTunState -Enabled $false
    }
}

if ($deploySucceeded -and -not $SkipAiCheck) {
    Write-Host "[ai] verifying provider after TUN restoration"
    & python $DeployScript --verify-ai-only
    if ($LASTEXITCODE -ne 0) {
        throw "Control AI connectivity check failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Scheme B deployment completed; TUN state restored to $originalTunState"
