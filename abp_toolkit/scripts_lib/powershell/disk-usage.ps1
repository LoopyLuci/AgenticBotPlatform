<#
name: disk-usage
description: How much space each folder under a path takes, largest first
params: -Path: the folder (default: current); -Top: how many folders (default 20)
safety: read
#>
[CmdletBinding()]
param([string]$Path = '.', [int]$Top = 20)
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $Path).Path
$rows = Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $sum = (Get-ChildItem -LiteralPath $_.FullName -File -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    [pscustomobject]@{ SizeGB = [math]::Round(($sum / 1GB), 2); Folder = $_.Name }
}
$files = (Get-ChildItem -LiteralPath $root -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
$rows += [pscustomobject]@{ SizeGB = [math]::Round(($files / 1GB), 2); Folder = '(files here)' }
$rows | Sort-Object SizeGB -Descending | Select-Object -First $Top | Format-Table -AutoSize | Out-String -Width 300
