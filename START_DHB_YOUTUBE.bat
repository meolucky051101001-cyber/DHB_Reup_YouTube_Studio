@echo off
setlocal
chcp 65001 >nul
title DHB Reup YouTube Studio - Launcher
cd /d "%~dp0"
set "APP_URL=http://127.0.0.1:8686"

netstat -ano | findstr ":8686 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    start "" "%APP_URL%"
    exit /b 0
)

if not exist "logs" mkdir "logs"

powershell.exe -NoProfile -Command "$root = (Get-Location).Path; $python = (Get-Command python.exe -ErrorAction Stop).Source; $process = Start-Process -FilePath $python -ArgumentList 'app.py' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'logs\server.out.log') -RedirectStandardError (Join-Path $root 'logs\server.error.log') -PassThru; $ready = $false; for ($i = 0; $i -lt 40; $i++) { if ($process.HasExited) { break }; try { $response = Invoke-WebRequest -UseBasicParsing -Uri ($env:APP_URL + '/') -TimeoutSec 1; if ($response.StatusCode -eq 200) { $ready = $true; break } } catch { }; Start-Sleep -Milliseconds 500 }; if (-not $ready) { exit 1 }; Start-Process $env:APP_URL"
if errorlevel 1 (
    echo.
    echo Khong the khoi dong server. Xem logs\server.error.log de biet loi.
    pause
    exit /b 1
)
exit /b 0
