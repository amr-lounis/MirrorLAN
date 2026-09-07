@echo off
rem Build LANServer.exe from main.py (one file, GUI, no console window).
rem Needs Python + internet once for PyInstaller. Run: build.bat
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo Installing PyInstaller...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo [ERROR] Cannot install PyInstaller. Check internet and retry.
    pause
    exit /b 1
  )
)

if not exist www (
  echo [WARN] www folder missing - it will be created empty at runtime.
)

rem NOTE: --windowed (no console). All output uses a crash-safe printer,
rem so the app never dies for missing stdout. Status shows in the GUI.
python -m PyInstaller --noconfirm --clean --onefile --windowed --name LANServer --icon app.ico --add-data "www;www" main.py
if errorlevel 1 (
  echo [ERROR] Build failed.
  pause
  exit /b 1
)

echo.
echo [OK] dist\LANServer.exe
echo First run creates cert.pem and key.pem next to the exe automatically.
echo Double-click runs the GUI with no console window.
pause
