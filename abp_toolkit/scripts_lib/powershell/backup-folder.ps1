<#
name: backup-folder
description: Zip a folder into a timestamped archive and keep only the newest N archives
params: -Source: the folder to back up; -Destination: where archives go; -Keep: how many to keep (default 10)
safety: changes
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination, [int]$Keep = 10)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$src = (Resolve-Path -LiteralPath $Source).Path
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$name = (Split-Path $src -Leaf) + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip'
$zip = Join-Path $Destination $name
Compress-Archive -Path (Join-Path $src '*') -DestinationPath $zip -CompressionLevel Optimal
"Created $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)."
$old = Get-ChildItem -LiteralPath $Destination -Filter ((Split-Path $src -Leaf) + '-*.zip') | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($o in $old) { Remove-Item -LiteralPath $o.FullName -Force; "Removed old backup $($o.Name)." }
