@echo off
chcp 65001 >nul
title Cai Dat Thu Vien - DHB Reup YouTube Studio
cd /d "%~dp0"
echo =====================================================================
echo   CAI DAT THU VIEN PYTHON CHO TOOL DHB REUP YOUTUBE STUDIO
echo =====================================================================
echo Dang cai dat cac thu vien tu requirements.txt...
pip install -r requirements.txt
echo.
echo Dang cai dat trinh duyet Playwright Chromium...
python -m playwright install chromium
echo.
echo =====================================================================
echo   DA CAI DAT HOAN TAT! 
echo   Ban co the chay tool bang file: START_DHB_YOUTUBE.bat
echo =====================================================================
pause
