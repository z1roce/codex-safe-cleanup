@echo off
setlocal
set "CLEANUP_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CLEANUP_PYTHON%" goto run
set "CLEANUP_PYTHON=%LOCALAPPDATA%\Python\bin\python3.14.exe"
if exist "%CLEANUP_PYTHON%" goto run
set "CLEANUP_PYTHON=%LOCALAPPDATA%\Python\bin\python3.exe"
if exist "%CLEANUP_PYTHON%" goto run
echo Python 3.12+ required. Run safe_cleanup.py with an absolute Python path.
exit /b 2
:run
"%CLEANUP_PYTHON%" "%~dp0safe_cleanup.py" %*
exit /b %errorlevel%
