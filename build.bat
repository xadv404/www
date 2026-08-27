@echo off
echo.
echo  Building IMAP Account Checker...
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found in PATH
    pause
    exit /b 1
)

pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    pip install pyinstaller
)

pyinstaller --onefile --console --name "checker" --add-data "providers.py;." checker.py

if errorlevel 1 (
    echo.
    echo  [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo  Done! Binary at: dist\checker.exe
echo.
pause
