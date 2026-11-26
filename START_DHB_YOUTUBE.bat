@echo off
setlocal
chcp 65001 >nul
title DHB Reup YouTube Studio - Launcher
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0launch_dhb.ps1"
if errorlevel 1 (
    echo.
    echo Khong the khoi dong server. Xem logs\server.error.log de biet loi.
    pause
    exit /b 1
)
exit /b 0
