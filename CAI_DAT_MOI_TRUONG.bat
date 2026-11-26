@echo off
setlocal
chcp 65001 >nul
title Cai Dat Thu Vien - DHB Reup YouTube Studio
cd /d "%~dp0"
echo =====================================================================
echo   CAI DAT THU VIEN PYTHON CHO TOOL DHB REUP YOUTUBE STUDIO
echo =====================================================================
echo Dang cai dat cac thu vien tu requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Cai thu vien Python that bai. Xem loi ben tren.
    exit /b 1
)
echo.
echo Dang cai dat trinh duyet Playwright Chromium...
python -m playwright install chromium
if errorlevel 1 (
    echo Cai Playwright Chromium that bai. Xem loi ben tren.
    exit /b 1
)
python -B -c "from config import FFMPEG_EXE, FFPROBE_EXE; import subprocess; [subprocess.run([str(path), '-version'], stdout=subprocess.DEVNULL, check=True) for path in (FFMPEG_EXE, FFPROBE_EXE)]"
if errorlevel 1 (
    echo Khong chay duoc FFmpeg hoac FFprobe. Kiem tra DHB_FFMPEG, DHB_FFPROBE va PATH.
    exit /b 1
)
echo.
echo =====================================================================
echo   DA CAI DAT HOAN TAT! 
echo   Ban co the chay tool bang file: START_DHB_YOUTUBE.bat
echo =====================================================================
pause
