<#
name: clean-temp
description: Delete temporary files older than N days from your temp folders (shows what it would delete unless -Apply)
params: -Days: only files older than this (default 7); -Apply: really delete
safety: changes
#>
[CmdletBinding()]
param([int]$Days = 7, [switch]$Apply)
Set-StrictMode -Version Latest
$cutoff = (Get-Date).AddDays(-$Days)
$folders = @($env:TEMP, (Join-Path $env:LOCALAPPDATA 'Temp')) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
$total = 0; $count = 0
foreach ($f in $folders) {
    Get-ChildItem -LiteralPath $f -File -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff } | ForEach-Object {
        $total += $_.Length; $count++
        if ($Apply) { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
    }
}
$verb = if ($Apply) { 'Deleted' } else { 'Would delete' }
"$verb $count file(s), $([math]::Round($total / 1MB, 1)) MB, older than $Days day(s) in: $($folders -join ', ')"
if (-not $Apply) { 'Run again with -Apply to delete them.' }
