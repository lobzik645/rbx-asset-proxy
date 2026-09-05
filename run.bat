@echo off
cd /d "%~dp0"
if exist "RobloxProxy.exe" (
    start "" "RobloxProxy.exe"
) else (
    start "" pythonw tray_app.py
)
