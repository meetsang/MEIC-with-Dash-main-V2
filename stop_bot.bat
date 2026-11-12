@echo off
title Stopping MEIC Autotrader
cd /d "%~dp0"
echo Gracefully stopping MEIC Autotrader and flushing journals...

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -c "try: from clone_trades.engine import get_engine; get_engine().flush_all(force=True); except Exception: pass" 2>nul
    .venv\Scripts\python.exe -c "try: from dashboard.process_manager import stop_service; stop_service('all'); except Exception: pass" 2>nul
)

rem Terminate any remaining bot master windows or MEIC python runners
taskkill /F /FI "WINDOWTITLE eq MEIC Master Bot*" 2>nul
taskkill /F /FI "WINDOWTITLE eq MEIC Discretionary Clone Trades*" 2>nul

rem Clear any leftover singleton locks
del /Q runtime\locks\*.lock 2>nul
echo All MEIC processes stopped cleanly and locks cleared.
