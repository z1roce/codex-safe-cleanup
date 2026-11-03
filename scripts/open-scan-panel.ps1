$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-launcher.ps1')
$cleanupPython = Find-CleanupPython
& $cleanupPython -X utf8 -B (Join-Path $PSScriptRoot 'scan_panel.py') --open @args
exit $LASTEXITCODE
