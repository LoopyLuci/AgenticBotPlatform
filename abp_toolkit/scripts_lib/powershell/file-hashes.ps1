<#
name: file-hashes
description: Checksums of a file or of every file in a folder (SHA256 by default), for verifying downloads and copies
params: -Path: file or folder; -Algorithm: SHA256, SHA1, SHA512 or MD5
safety: read
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Path, [ValidateSet('SHA256', 'SHA1', 'SHA512', 'MD5')][string]$Algorithm = 'SHA256')
Set-StrictMode -Version Latest
$items = if ((Get-Item -LiteralPath $Path).PSIsContainer) { Get-ChildItem -LiteralPath $Path -File -Recurse } else { Get-Item -LiteralPath $Path }
$items | Get-FileHash -Algorithm $Algorithm | Select-Object Hash, @{ n = 'File'; e = { Resolve-Path -Relative $_.Path } } |
    Format-Table -AutoSize | Out-String -Width 400
