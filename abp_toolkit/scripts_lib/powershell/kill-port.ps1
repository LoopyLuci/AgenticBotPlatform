<#
name: kill-port
description: Stop the program that is listening on a TCP port (asks for -Force to actually stop it)
params: -Port: the port number; -Force: really stop it (without it, only says what would be stopped)
safety: changes
#>
[CmdletBinding()]
param([Parameter(Mandatory)][int]$Port, [switch]$Force)
Set-StrictMode -Version Latest
$pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
if (-not $pids) { "Nothing is listening on port $Port."; exit 0 }
foreach ($id in $pids) {
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    if ($Force) { Stop-Process -Id $id -Force; "Stopped $($p.ProcessName) (pid $id)." }
    else { "Would stop $($p.ProcessName) (pid $id, $($p.Path)). Run again with -Force to stop it." }
}
