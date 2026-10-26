@echo off
rem ---------------------------------------------------------------------------
rem Build RTL Viewer into a single portable executable (dist\RTLViewer.exe).
rem Requires: Python 3.12+ with the project venv activated or pyinstaller on PATH
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo [1/3] Installing build dependencies...
python -m pip install --quiet -r requirements.txt pyinstaller || goto :error

echo [2/3] Running PyInstaller...
python -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name RTLViewer ^
    --add-data "resources;resources" ^
    --hidden-import PySide6.QtNetwork ^
    --collect-all keyboard ^
    --collect-all pyperclip ^
    --collect-all markdown_it ^
    --collect-all mdit_py_plugins ^
    --collect-all linkify_it ^
    --collect-all pygments ^
    main.py || goto :error

echo [3/3] Done.
echo.
echo    Executable: dist\RTLViewer.exe
echo    (one-file build; first launch is slightly slower due to self-extraction)
goto :eof

:error
echo.
echo Build FAILED - see the messages above.
exit /b 1
