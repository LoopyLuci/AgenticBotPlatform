<#
name: installed-apps
description: Installed programs with version and publisher (from the uninstall registry), optionally filtered by name
params: -Filter: part of a name to match
safety: read
#>
[CmdletBinding()]
param([string]$Filter = '')
Set-StrictMode -Version Latest
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
Get-ItemProperty $keys -ErrorAction SilentlyContinue |
    Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName -and $_.DisplayName -like "*$Filter*" } |
    Sort-Object DisplayName -Unique |
    Select-Object DisplayName, DisplayVersion, Publisher |
    Format-Table -AutoSize | Out-String -Width 300
