<#
name: keep-awake
description: Keep this computer (and optionally its display) from sleeping for N minutes, or until stopped with Ctrl+C
params: -Minutes: how long (0 = until stopped); -Display: keep the screen on too
safety: changes
#>
[CmdletBinding()]
param([int]$Minutes = 60, [switch]$Display)
Set-StrictMode -Version Latest
Add-Type -Namespace Win32 -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);'
$ES_CONTINUOUS = [uint32]'0x80000000'; $ES_SYSTEM_REQUIRED = [uint32]1; $ES_DISPLAY_REQUIRED = [uint32]2
$flags = $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED
if ($Display) { $flags = $flags -bor $ES_DISPLAY_REQUIRED }
[void][Win32.Power]::SetThreadExecutionState($flags)
$until = if ($Minutes -gt 0) { (Get-Date).AddMinutes($Minutes) } else { [datetime]::MaxValue }
"Keeping the computer awake" + $(if ($Display) { ' with the display on' } else { '' }) + $(if ($Minutes -gt 0) { " until $until" } else { ' until stopped' }) + '.'
try { while ((Get-Date) -lt $until) { Start-Sleep -Seconds 30 } }
finally { [void][Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS); 'Sleep allowed again.' }
