$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-launcher.ps1')
$cleanupPython = Find-CleanupPython
& $cleanupPython -X utf8 -B (Join-Path $PSScriptRoot 'safe_cleanup.py') @args
exit $LASTEXITCODE
