@echo off
setlocal
chcp 65001 >nul
title DHB Reup YouTube Studio - Launcher
cd /d "%~dp0"
set "APP_URL=http://127.0.0.1:8686"

if not exist "logs" mkdir "logs"

powershell.exe -NoProfile -Command "$root = (Get-Location).Path; function Test-Dhb { try { $health = Invoke-RestMethod -Uri ($env:APP_URL + '/health/dhb') -TimeoutSec 1; return ($health.application -eq 'dhb-reup-youtube-studio' -and $health.source_root -eq $root) } catch { return $false } }; if (Test-Dhb) { Start-Process $env:APP_URL; exit 0 }; if (Get-NetTCPConnection -LocalPort 8686 -State Listen -ErrorAction SilentlyContinue) { Write-Host 'Cong 8686 dang duoc dung boi ban DHB cu hoac ung dung khac.'; exit 1 }; $python = (Get-Command python.exe -ErrorAction Stop).Source; $process = Start-Process -FilePath $python -ArgumentList 'app.py' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'logs\server.out.log') -RedirectStandardError (Join-Path $root 'logs\server.error.log') -PassThru; for ($i = 0; $i -lt 40; $i++) { if ($process.HasExited) { break }; if (Test-Dhb) { Start-Process $env:APP_URL; exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
    echo.
    echo Khong the khoi dong server. Xem logs\server.error.log de biet loi.
    pause
    exit /b 1
)
exit /b 0
