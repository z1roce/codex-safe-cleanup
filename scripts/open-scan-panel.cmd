@echo off
setlocal
if defined CODEX_CLEANUP_PYTHON if exist "%CODEX_CLEANUP_PYTHON%" goto custom
set "CLEANUP_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CLEANUP_PYTHON%" goto run
set "CLEANUP_PYTHON=%LOCALAPPDATA%\Python\bin\python3.14.exe"
if exist "%CLEANUP_PYTHON%" goto run
set "CLEANUP_PYTHON=%LOCALAPPDATA%\Python\bin\python3.exe"
if exist "%CLEANUP_PYTHON%" goto run
where py >nul 2>nul
if not errorlevel 1 goto launcher
where python >nul 2>nul
if not errorlevel 1 goto pathpython
echo Python 3.12+ required. Set CODEX_CLEANUP_PYTHON to its full path.
pause
exit /b 2
:custom
set "CLEANUP_PYTHON=%CODEX_CLEANUP_PYTHON%"
:run
"%CLEANUP_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 2)" >nul 2>nul
if errorlevel 1 goto incompatible
"%CLEANUP_PYTHON%" "%~dp0scan_panel.py" --open
goto done
:launcher
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 2)" >nul 2>nul
if errorlevel 1 goto incompatible
py -3 "%~dp0scan_panel.py" --open
goto done
:pathpython
python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 2)" >nul 2>nul
if errorlevel 1 goto incompatible
python "%~dp0scan_panel.py" --open
goto done
:incompatible
echo Python 3.12+ required. Set CODEX_CLEANUP_PYTHON to its full path.
pause
exit /b 2
:done
set "CLEANUP_EXIT=%errorlevel%"
if not "%CLEANUP_EXIT%"=="0" pause
exit /b %CLEANUP_EXIT%
