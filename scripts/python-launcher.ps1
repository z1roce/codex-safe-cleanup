# Shared by both public entry points. Probe automatic candidates independently.
function Find-CleanupPython {
    param([string[]]$Candidates)
    $explicit = $env:CODEX_CLEANUP_PYTHON
    if ($explicit) { $Candidates = @($explicit) }
    if (!$Candidates) {
        $Candidates = @(
            (Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime\python.exe'),
            (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Python\bin\python3.exe')
        )
        foreach ($launcher in @(Get-Command py.exe -All -ErrorAction SilentlyContinue)) {
            try {
                $lines = & $launcher.Source -0p 2>$null
                foreach ($line in $lines) {
                    if ($line -match '([A-Za-z]:\\.+\.exe)\s*$') { $Candidates += $Matches[1] }
                }
            } catch { Write-Verbose $_ }
        }
        foreach ($name in @('python.exe','python3.exe')) {
            $Candidates += @(Get-Command $name -All -ErrorAction SilentlyContinue | ForEach-Object Source)
        }
        foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'),(Join-Path $env:LOCALAPPDATA 'Python'))) {
            if (Test-Path -LiteralPath $root) {
                $folders = @($root) + @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
                foreach ($folder in $folders) {
                    $Candidates += @(Get-ChildItem -LiteralPath $folder -Filter 'python*.exe' -File -ErrorAction SilentlyContinue | ForEach-Object FullName)
                }
            }
        }
        foreach ($registryRoot in @('HKCU:\Software\Python','HKLM:\Software\Python','HKLM:\Software\WOW6432Node\Python')) {
            foreach ($vendor in @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
                foreach ($version in @(Get-ChildItem -LiteralPath $vendor.PSPath -ErrorAction SilentlyContinue)) {
                    $install = Get-Item -LiteralPath ($version.PSPath+'\InstallPath') -ErrorAction SilentlyContinue
                    if ($install) {
                        $executable = $install.GetValue('ExecutablePath')
                        if (!$executable -and $install.GetValue('')) { $executable = Join-Path ($install.GetValue('')) 'python.exe' }
                        if ($executable) { $Candidates += $executable }
                    }
                }
            }
        }
    }
    foreach ($candidate in @($Candidates | Select-Object -Unique)) {
        if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $probe = New-Object System.Diagnostics.Process
        $probe.StartInfo.FileName = $candidate
        $probe.StartInfo.Arguments = '-I -c "import sys,json; print(json.dumps([sys.executable,sys.version.split()[0]])); sys.exit(0 if sys.version_info >= (3,12) else 2)"'
        $probe.StartInfo.UseShellExecute = $false
        $probe.StartInfo.CreateNoWindow = $true
        $probe.StartInfo.RedirectStandardOutput = $true
        $probe.StartInfo.RedirectStandardError = $true
        try {
            [void]$probe.Start()
            if (!$probe.WaitForExit(5000)) { $probe.Kill(); continue }
            if ($probe.ExitCode -ne 0) { continue }
            $details = $probe.StandardOutput.ReadToEnd() | ConvertFrom-Json
            Write-Host ("Python {0}  {1}" -f $details[1], $details[0])
            return [string]$details[0]
        } catch { Write-Verbose $_ } finally { $probe.Dispose() }
    }
    if ($explicit) { throw "CODEX_CLEANUP_PYTHON is invalid, cannot run, or is below Python 3.12: $explicit" }
    throw 'Python 3.12+ was not found. Set CODEX_CLEANUP_PYTHON to a working executable path.'
}
