@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-scan-panel.ps1" %*
set "CLEANUP_EXIT=%errorlevel%"
if not "%CLEANUP_EXIT%"=="0" pause
exit /b %CLEANUP_EXIT%
