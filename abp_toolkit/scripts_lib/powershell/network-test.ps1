<#
name: network-test
description: Check hosts: DNS lookup, ping time, and whether TCP ports are open
params: -Hosts: host names or addresses, comma-separated (default: github.com,1.1.1.1); -Ports: TCP ports, comma-separated (default 443)
safety: network
#>
[CmdletBinding()]
param([string]$Hosts = 'github.com,1.1.1.1', [string]$Ports = '443')
Set-StrictMode -Version Latest
# Comma-separated strings, because arrays cannot be passed to a script run with -File.
$hostList = @($Hosts -split '[,\s]+' | Where-Object { $_ })
$portList = @($Ports -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { [int]$_ })
foreach ($h in $hostList) {
    $ip = try { ([System.Net.Dns]::GetHostAddresses($h) | Select-Object -First 1).IPAddressToString } catch { 'DNS failed' }
    $ping = Test-Connection -ComputerName $h -Count 2 -ErrorAction SilentlyContinue
    # Windows PowerShell names the round-trip time ResponseTime, PowerShell 7 names it Latency.
    $times = @($ping | ForEach-Object { if ($_.PSObject.Properties['Latency']) { $_.Latency } else { $_.ResponseTime } })
    $ms = if ($ping -and $times) { [math]::Round(($times | Measure-Object -Average).Average, 1) } else { $null }
    $open = foreach ($p in $portList) {
        $c = New-Object System.Net.Sockets.TcpClient
        try { $ok = $c.ConnectAsync($h, $p).Wait(2000) -and $c.Connected } catch { $ok = $false } finally { $c.Close() }
        "${p}:" + $(if ($ok) { 'open' } else { 'closed' })
    }
    [pscustomobject]@{ Host = $h; Address = $ip; PingMs = $(if ($ms) { $ms } else { 'no reply' }); Ports = ($open -join ' ') }
}
