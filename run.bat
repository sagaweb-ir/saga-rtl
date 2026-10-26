@echo off
rem ---------------------------------------------------------------------------
rem RTL Viewer - double-click launcher.
rem
rem - First run (no .venv yet): creates the venv and installs dependencies.
rem - Every run: starts the app WITHOUT a console window (pythonw) with the
rem   tray icon; quit it later from the tray menu.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: preparing the environment - this may take a few minutes...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -X utf8 main.py
) else (
    start "" ".venv\Scripts\python.exe" -X utf8 main.py
)
exit /b 0

:error
echo.
echo Something went wrong - see the messages above.
echo Make sure Python 3.12+ is installed and on PATH.
pause
exit /b 1
