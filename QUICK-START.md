# ⚡ MEIC Autotrader - Quick Start for New Server

**Copy this entire message into VS Code Copilot Chat on your new server tomorrow:**

---

I have a Charles Schwab 0DTE SPX options autotrader project that I need to set up and run on this Windows server.

## What I Need You To Do:

1. **Verify prerequisites are installed:**
   - Python 3.10+ 
   - Git
   - Mosquitto MQTT broker (if not installed: `winget install EclipseFoundation.Mosquitto`)

2. **Help me clone the repository and set up the environment:**
   - Create virtual environment
   - Install dependencies from requirements.txt
   - Verify Mosquitto service is running

3. **Configure credentials in `common/auth/config.py`:**
   - CLIENT_ID: `QEihsG3zJ1k4Two5RwZ5ltBytUPg19oV`
   - CLIENT_SECRET: (I'll provide this)
   - P_ACCT: `44952712`
   - Callback URL must be: `https://127.0.0.1:8080/callback`

4. **Generate Schwab auth token:**
   - Run `python generate_token.py` from `common/auth/` directory
   - Guide me through the OAuth flow
   - Verify `common/auth/token.json` is created

5. **Verify directory structure:**
   - Ensure `meic0dte/logs/` exists
   - Check all config files are present

6. **Start the bot for tomorrow's trading:**
   - Command: `python run.py`
   - Dashboard: http://localhost:5001
   - Verify streaming starts at 8:30 AM CT
   - Verify first tranche opens at 10:59 AM CT

## Key Files:
- `run.py` — Main launcher (single command to start everything)
- `common/auth/config.py` — Schwab credentials
- `common/auth/token.json` — Auth token (auto-generated)
- `meic0dte/app/config.py` — QUANTITY=1 (contracts per spread)
- `launcher.log` — Main activity log

## Project Architecture:
```
run.py launches:
├── dashboard/server.py (Flask dashboard on port 5001)
├── streaming/publish.py (Schwab WebSocket → MQTT)
└── meic0dte/app_main.py (Trading engine - 6 tranches)
```

## Trading Schedule (Central Time):
- 8:30 AM — Streaming starts
- 10:59 AM — Tranche 1
- 11:59 AM — Tranche 2  
- 12:29 PM — Tranche 3
- 1:14 PM — Tranche 4
- 1:44 PM — Tranche 5
- 1:59 PM — Tranche 6
- 3:00 PM — Everything stops

## Important Notes:
- Bot auto-skips weekends, NYSE holidays, and FOMC days
- Token auto-refreshes every 25 minutes
- All times are Central Time (CT)
- Currently trades 1 contract per spread (safety mode)
- Emergency stop: `python meic0dte/meickillswitch.py`

## Full setup documentation is in: `SERVER-SETUP-PROMPT.md`

**Please guide me through the complete setup process step-by-step, verifying each step before moving to the next.**

---

**Start by checking Python version and if Git is installed.**
