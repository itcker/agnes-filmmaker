@echo off
chcp 65001 >nul
title agnes-filmmaker 画布监制台
cd /d "%~dp0"
echo.
echo   正在启动 agnes-filmmaker 画布监制台...
echo   首次运行请先：pip install -r requirements.txt
echo   并在同目录 .env 里填入 AGNES_KEY=sk-xxxx
echo.
python -m web.app
pause
