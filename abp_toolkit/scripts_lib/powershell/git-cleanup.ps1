<#
name: git-cleanup
description: In a git repository, list (or with -Apply delete) local branches already merged into the main branch, and prune gone remote branches
params: -Main: the main branch (default: main, or master if there is no main); -Apply: really delete
safety: changes
#>
[CmdletBinding()]
param([string]$Main = '', [switch]$Apply)
Set-StrictMode -Version Latest
if (-not (git rev-parse --is-inside-work-tree 2>$null)) { throw 'Not inside a git repository.' }
if (-not $Main) {
    git show-ref --verify --quiet refs/heads/main
    $Main = if ($LASTEXITCODE -eq 0) { 'main' } else { 'master' }
}
git fetch --prune --quiet
$current = git rev-parse --abbrev-ref HEAD
$merged = git branch --merged $Main --format='%(refname:short)' | Where-Object { $_ -and $_ -ne $Main -and $_ -ne $current }
if (-not $merged) { "No merged branches to remove."; exit 0 }
foreach ($b in $merged) { if ($Apply) { git branch -d $b } else { "Would delete $b" } }
if (-not $Apply) { 'Run again with -Apply to delete them.' }
