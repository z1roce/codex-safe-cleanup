$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-launcher.ps1')
$cleanupPython = Find-CleanupPython
& $cleanupPython -B (Join-Path $PSScriptRoot 'safe_cleanup.py') @args
exit $LASTEXITCODE
