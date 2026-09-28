<#
name: download-retry
description: Download a file with retries and an optional SHA256 check
params: -Url: what to download; -OutFile: where to save it; -Retries: attempts (default 4); -Sha256: expected checksum
safety: network
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$OutFile, [int]$Retries = 4, [string]$Sha256 = '')
Set-StrictMode -Version Latest
$ProgressPreference = 'SilentlyContinue'
for ($i = 1; $i -le $Retries; $i++) {
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 600
        if ($Sha256) {
            $got = (Get-FileHash $OutFile -Algorithm SHA256).Hash
            if ($got -ne $Sha256.ToUpper()) { Remove-Item $OutFile -Force; throw "checksum mismatch: $got" }
        }
        "Downloaded $OutFile ($([math]::Round((Get-Item $OutFile).Length / 1MB, 2)) MB) on attempt $i."
        exit 0
    } catch {
        "Attempt $i failed: $($_.Exception.Message)"
        if ($i -lt $Retries) { Start-Sleep -Seconds ([math]::Pow(2, $i)) }
    }
}
exit 1
