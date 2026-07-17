@echo off
REM Launcher for the Mouse Thermo live monitor.
REM Double-clickable; also the target of the desktop shortcut.

REM cd to the repo root regardless of where this was invoked from. The app
REM resolves config, zigbee.db and the default recordings/ folder relative to
REM the working directory, so this must not be left as the caller's cwd
REM (double-clicking a shortcut can start you in system32).
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo(
    echo ERROR: virtualenv not found at:
    echo   %PY%
    echo(
    echo Create it and install deps first:
    echo   python -m venv .venv
    echo   .venv\Scripts\python -m pip install -r mouse_thermo\requirements-gui.txt
    echo(
    pause
    exit /b 1
)

set "CFG=%~dp0mouse_thermo\config.local.yaml"
if not exist "%CFG%" (
    echo(
    echo ERROR: config not found at:
    echo   %CFG%
    echo(
    echo config.local.yaml is gitignored ^(it holds this rig's real IEEE
    echo addresses and COM ports^). Copy mouse_thermo\config.yaml to
    echo mouse_thermo\config.local.yaml and fill it in.
    echo(
    pause
    exit /b 1
)

echo Starting Mouse Thermo live monitor...
echo   config: %CFG%
echo(
echo Keep this window open -- it shows the live log. Closing it stops the
echo session ^(the lamp is commanded OFF on the way out^).
echo(

"%PY%" -m mouse_thermo.gui --config "%CFG%"
set "RC=%ERRORLEVEL%"

REM Only hold the window open on failure, so a normal close is not annoying,
REM but a crash is still readable instead of vanishing.
if not "%RC%"=="0" (
    echo(
    echo ============================================================
    echo  The session exited with error code %RC%.
    echo  The log above shows why. This window is kept open so the
    echo  error is readable -- press a key to close it.
    echo ============================================================
    pause
)
exit /b %RC%
