<#
name: event-log-errors
description: Errors and critical events from the Windows System and Application logs in the last N hours, grouped by source
params: -Hours: how far back (default 24); -Top: how many sources (default 15)
safety: read
#>
[CmdletBinding()]
param([int]$Hours = 24, [int]$Top = 15)
Set-StrictMode -Version Latest
$since = (Get-Date).AddHours(-$Hours)
$events = Get-WinEvent -FilterHashtable @{ LogName = 'System', 'Application'; Level = 1, 2; StartTime = $since } -ErrorAction SilentlyContinue
if (-not $events) { "No errors in the last $Hours hour(s)."; exit 0 }
$events | Group-Object ProviderName | Sort-Object Count -Descending | Select-Object -First $Top | ForEach-Object {
    $last = $_.Group | Sort-Object TimeCreated -Descending | Select-Object -First 1
    [pscustomobject]@{ Count = $_.Count; Source = $_.Name; Last = $last.TimeCreated; Message = (($last.Message -split "`n")[0]).Trim() }
} | Format-Table -AutoSize -Wrap | Out-String -Width 300
