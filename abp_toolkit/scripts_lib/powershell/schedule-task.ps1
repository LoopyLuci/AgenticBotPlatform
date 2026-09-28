<#
name: schedule-task
description: Create (or remove) a Windows scheduled task that runs a program daily at a time, or at logon
params: -Name: task name; -Program: what to run; -Arguments: its arguments; -At: time like 03:00 (or 'logon'); -Remove: delete the task
safety: changes
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Name, [string]$Program, [string]$Arguments = '', [string]$At = '03:00', [switch]$Remove)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Remove) { Unregister-ScheduledTask -TaskName $Name -Confirm:$false; "Removed task $Name."; exit 0 }
if (-not $Program) { throw 'Give -Program' }
$action = if ($Arguments) { New-ScheduledTaskAction -Execute $Program -Argument $Arguments } else { New-ScheduledTaskAction -Execute $Program }
$trigger = if ($At -eq 'logon') { New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME } else { New-ScheduledTaskTrigger -Daily -At $At }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
"Task $Name runs $Program " + $(if ($At -eq 'logon') { 'at logon.' } else { "daily at $At." })
