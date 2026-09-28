<#
name: user-path
description: List, add or remove folders on your user PATH (no admin needed; new terminals see the change)
params: -Action: list, add or remove; -Folder: the folder to add or remove
safety: changes
#>
[CmdletBinding()]
param([ValidateSet('list', 'add', 'remove')][string]$Action = 'list', [string]$Folder)
Set-StrictMode -Version Latest
$current = [Environment]::GetEnvironmentVariable('Path', 'User')
$parts = @($current -split ';' | Where-Object { $_ })
switch ($Action) {
    'list' { $parts | ForEach-Object { '{0} {1}' -f $(if (Test-Path $_) { ' ' } else { '!' }), $_ }; '(! = folder does not exist)' }
    'add' {
        if (-not $Folder) { throw 'Give -Folder' }
        $full = (Resolve-Path -LiteralPath $Folder).Path
        if ($parts -contains $full) { "$full is already on PATH."; break }
        [Environment]::SetEnvironmentVariable('Path', (($parts + $full) -join ';'), 'User'); "Added $full."
    }
    'remove' {
        if (-not $Folder) { throw 'Give -Folder' }
        $kept = $parts | Where-Object { $_.TrimEnd('\') -ne $Folder.TrimEnd('\') }
        if ($kept.Count -eq $parts.Count) { "$Folder is not on PATH."; break }
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User'); "Removed $Folder."
    }
}
