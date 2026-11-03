$ErrorActionPreference = 'Stop'
# Avoid the Python 3.5 executable that may appear first on this computer's PATH.
$cleanupCandidates = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Python\bin\python3.14.exe'),
    (Join-Path $env:LOCALAPPDATA 'Python\bin\python3.exe')
)
$cleanupPython = $null
foreach ($candidate in $cleanupCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $cleanupPython = $candidate; break }
    }
}
if (-not $cleanupPython) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($candidate) {
        & $candidate.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $cleanupPython = $candidate.Source }
    }
}
if (-not $cleanupPython) { throw '需要 Python 3.12+。请安装后用其绝对路径运行 safe_cleanup.py；本脚本不联网安装。' }
& $cleanupPython (Join-Path $PSScriptRoot 'safe_cleanup.py') @args
exit $LASTEXITCODE
