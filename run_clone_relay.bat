@echo off
title MEIC Discretionary Clone Trades - Secure Relay
cd /d "%~dp0"
echo ============================================================================
echo   Starting MEIC Discretionary Clone Trades Secure Relay (Port 5005)
echo ============================================================================
uv run python clone_trades/relay.py --ngrok
pause
