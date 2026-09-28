<#
name: port-owner
description: Which program is listening on (or connected through) a TCP or UDP port
params: -Port: the port number
safety: read
#>
[CmdletBinding()]
param([Parameter(Mandatory)][int]$Port)
Set-StrictMode -Version Latest
$rows = @()
$rows += Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ Protocol = 'TCP'; Local = "$($_.LocalAddress):$($_.LocalPort)"; Remote = "$($_.RemoteAddress):$($_.RemotePort)"; State = $_.State; PID = $_.OwningProcess }
}
$rows += Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ Protocol = 'UDP'; Local = "$($_.LocalAddress):$($_.LocalPort)"; Remote = ''; State = ''; PID = $_.OwningProcess }
}
if (-not $rows) { "Nothing is using port $Port."; exit 0 }
$rows | ForEach-Object {
    $p = Get-Process -Id $_.PID -ErrorAction SilentlyContinue
    $_ | Add-Member Program ($(if ($p) { $p.ProcessName } else { '?' })) -PassThru | Add-Member Path ($(if ($p) { $p.Path } else { '' })) -PassThru
} | Format-Table -AutoSize | Out-String -Width 300
