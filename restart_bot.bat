@echo off
title Restarting MEIC Autotrader
cd /d "%~dp0"
echo ========================================================
echo Stopping existing MEIC services...
echo ========================================================
call stop_bot.bat
timeout /t 1 /nobreak >nul
echo ========================================================
echo Starting MEIC Autotrader (24/7 Continuous Master Runner)
echo ========================================================
call start_bot.bat
