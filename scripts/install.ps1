param([string]$Destination = (Join-Path $env:USERPROFILE '.agents\skills\codex-safe-cleanup'), [switch]$Upgrade, [switch]$Recover)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-launcher.ps1')
$cleanupPython = Find-CleanupPython
$cleanupArgs = @('--destination', $Destination)
if ($Upgrade) { $cleanupArgs += '--upgrade' }
if ($Recover) { $cleanupArgs += '--recover' }
& $cleanupPython -B (Join-Path $PSScriptRoot 'install_skill.py') @cleanupArgs
exit $LASTEXITCODE
