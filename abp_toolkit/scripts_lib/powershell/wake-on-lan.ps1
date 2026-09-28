<#
name: wake-on-lan
description: Wake a sleeping or powered-off computer on the network by sending it a Wake-on-LAN magic packet
params: -Mac: its network adapter's MAC address (AA:BB:CC:DD:EE:FF); -Broadcast: broadcast address (default 255.255.255.255); -Port: 9 or 7
safety: network
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Mac, [string]$Broadcast = '255.255.255.255', [int]$Port = 9)
Set-StrictMode -Version Latest
$clean = $Mac -replace '[^0-9A-Fa-f]', ''
if ($clean.Length -ne 12) { throw "Not a MAC address: $Mac" }
$macBytes = [byte[]] -split ($clean -replace '..', '0x$& ')
$packet = [byte[]](,0xFF * 6) + ($macBytes * 16)
$udp = New-Object System.Net.Sockets.UdpClient
try {
    $udp.EnableBroadcast = $true
    [void]$udp.Send($packet, $packet.Length, $Broadcast, $Port)
    "Sent a magic packet for $Mac to ${Broadcast}:$Port."
} finally { $udp.Close() }
