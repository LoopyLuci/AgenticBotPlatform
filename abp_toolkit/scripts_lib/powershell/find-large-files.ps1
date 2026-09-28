<#
name: find-large-files
description: The biggest files under a folder
params: -Path: folder to search (default: current); -Top: how many (default 25); -MinMB: smallest size to list (default 10)
safety: read
#>
[CmdletBinding()]
param([string]$Path = '.', [int]$Top = 25, [double]$MinMB = 10)
Set-StrictMode -Version Latest
Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -ge $MinMB * 1MB } |
    Sort-Object Length -Descending |
    Select-Object -First $Top @{ n = 'SizeMB'; e = { [math]::Round($_.Length / 1MB, 1) } }, LastWriteTime, FullName |
    Format-Table -AutoSize | Out-String -Width 400
