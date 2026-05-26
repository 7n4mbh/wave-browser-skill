@echo off
:: wave-browser Windows launcher -- ensures the skill-local Python venv is
:: set up, then dispatches the CLI. Agents invoke this script (not the .py
:: directly) so the runtime is self-bootstrapping.
:: Mirrors the behavior of the wave-browser bash launcher on Linux/macOS.

setlocal
set "SCRIPT_DIR=%~dp0"
set "SKILL_DIR=%~dp0.."
if defined WAVE_BROWSER_VENV (
    set "VENV_DIR=%WAVE_BROWSER_VENV%"
) else (
    set "VENV_DIR=%SKILL_DIR%\.venv"
)

if not exist "%VENV_DIR%\.installed" (
    call "%SCRIPT_DIR%bootstrap.bat" >&2
    if errorlevel 1 (
        echo [wave-browser] bootstrap failed 1>&2
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [wave-browser] venv python not found at %VENV_PY% 1>&2
    exit /b 1
)

"%VENV_PY%" "%SCRIPT_DIR%wave_browser.py" %*
endlocal
exit /b %ERRORLEVEL%
