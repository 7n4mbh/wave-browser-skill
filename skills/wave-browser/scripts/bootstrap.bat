@echo off
:: bootstrap.bat -- create the local Python venv used by wave-browser (Windows)
:: Idempotent: safe to re-run. The wave-browser launcher calls this on first
:: use, so an agent does not normally need to invoke it directly.
:: Mirrors the behavior of bootstrap.sh on Linux/macOS.

setlocal
set "SCRIPT_DIR=%~dp0"
set "SKILL_DIR=%~dp0.."
if defined WAVE_BROWSER_VENV (
    set "VENV_DIR=%WAVE_BROWSER_VENV%"
) else (
    set "VENV_DIR=%SKILL_DIR%\.venv"
)
set "REQ_FILE=%SKILL_DIR%\requirements.txt"
set "STAMP=%VENV_DIR%\.installed"
if defined WAVE_BROWSER_PYTHON (
    set "PY=%WAVE_BROWSER_PYTHON%"
) else (
    set "PY=python"
)

where %PY% >nul 2>nul
if errorlevel 1 (
    echo [wave-browser] %PY% not found on PATH. Install Python 3.10+. 1>&2
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [wave-browser] need Python ^>= 3.10 1>&2
    exit /b 1
)

if not exist "%VENV_DIR%" (
    echo [wave-browser] creating venv at %VENV_DIR% 1>&2
    %PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [wave-browser] failed to create venv 1>&2
        exit /b 1
    )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [wave-browser] venv python not found at %VENV_PY% 1>&2
    exit /b 1
)

if not exist "%STAMP%" (
    echo [wave-browser] installing Python dependencies 1>&2
    "%VENV_PY%" -m pip install --quiet --upgrade pip
    if errorlevel 1 exit /b 1
    "%VENV_PY%" -m pip install --quiet -r "%REQ_FILE%"
    if errorlevel 1 exit /b 1
    echo [wave-browser] installing Chromium for Playwright 1>&2
    "%VENV_PY%" -m playwright install chromium
    if errorlevel 1 exit /b 1
    echo installed > "%STAMP%"
)

echo [wave-browser] ready (venv: %VENV_DIR%)
endlocal
exit /b 0
