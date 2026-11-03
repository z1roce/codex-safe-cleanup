$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-launcher.ps1')
$cleanupPython = Find-CleanupPython
& $cleanupPython -B (Join-Path $PSScriptRoot 'scan_panel.py') --open @args
exit $LASTEXITCODE
