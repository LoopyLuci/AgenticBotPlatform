<#
name: system-info
description: This computer at a glance: OS, CPU, memory, GPUs, disks, network adapters, uptime (as JSON with -Json)
params: -Json: output JSON instead of a table
safety: read
#>
[CmdletBinding()]
param([switch]$Json)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$info = [ordered]@{
    Computer = $env:COMPUTERNAME
    OS       = "$($os.Caption) $($os.Version) ($($os.OSArchitecture))"
    Uptime   = ((Get-Date) - $os.LastBootUpTime).ToString('d\.hh\:mm\:ss')
    CPU      = "$($cpu.Name.Trim()) - $($cpu.NumberOfCores) cores / $($cpu.NumberOfLogicalProcessors) threads"
    MemoryGB = [ordered]@{ Total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1); Free = [math]::Round($os.FreePhysicalMemory / 1MB, 1) }
    GPUs     = @(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) (driver $($_.DriverVersion))" })
    Disks    = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
            [ordered]@{ Drive = $_.DeviceID; SizeGB = [math]::Round($_.Size / 1GB, 1); FreeGB = [math]::Round($_.FreeSpace / 1GB, 1) } })
    Network  = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne '127.0.0.1' } |
            ForEach-Object { "$($_.InterfaceAlias): $($_.IPAddress)/$($_.PrefixLength)" })
}
if ($Json) { $info | ConvertTo-Json -Depth 4 } else { $info.GetEnumerator() | ForEach-Object { '{0,-10} {1}' -f $_.Key, (($_.Value | ConvertTo-Json -Compress -Depth 4) -replace '^"|"$') } }
