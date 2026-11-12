@echo off
title MEIC Master Bot (24/7 Continuous Mode)
cd /d "%~dp0"
echo ========================================================
echo Starting MEIC Autotrader (24/7 Continuous Master Runner)
echo ========================================================
echo Dashboard: http://localhost:5002
echo Webhook Relay: Port 5005
echo Mode: 24/7 (Daytime + Overnight Futures + Auto-Scheduler)
echo.
.venv\Scripts\python.exe run.py
pause
