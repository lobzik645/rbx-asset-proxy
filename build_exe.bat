@echo off
title Build RobloxProxy Tray App
cd /d "%~dp0"
echo ===================================================
echo Building standalone RobloxProxy.exe (Tray App)...
echo ===================================================
python -m PyInstaller --onefile --windowed --clean --icon="icon.ico" --name="RobloxProxy" tray_app.py
echo.
if exist "dist\RobloxProxy.exe" (
    copy /y "dist\RobloxProxy.exe" "RobloxProxy.exe"
    echo ===================================================
    echo SUCCESS! Standalone tray app created at:
    echo %~dp0RobloxProxy.exe
    echo ===================================================
) else (
    echo ===================================================
    echo Build failed. Check the error log above.
    echo ===================================================
)
pause
