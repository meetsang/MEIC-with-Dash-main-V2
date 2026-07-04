# 🚀 MEIC Autotrader - Server Setup Instructions

**Deploy this automated 0DTE SPX options trading bot on a new Windows server**

---

## Project Overview

This is a **fully automated Charles Schwab 0DTE SPX options spread trader** with a live web dashboard. It:
- Trades 0DTE SPX credit spreads (puts + calls) across up to 6 daily tranches
- Connects to Schwab's WebSocket feed for real-time SPX/VIX prices via MQTT
- Automatically skips NYSE holidays and FOMC days
- Refreshes Schwab auth tokens automatically every 25 minutes
- Serves a web dashboard at `http://localhost:5001` with live P&L, trade status, and logs
- Runs Monday–Friday with a single `python run.py` command

---

## Prerequisites Required on New Server

1. **Windows 10/11** (tested and working)
2. **Python 3.10+** (currently using Python 3.13)
3. **Git** (to clone the repository)
4. **Mosquitto MQTT Broker** (for streaming data)
5. **Schwab Developer Account** with approved app
6. **Schwab Brokerage Account** with options trading enabled

---

## Step-by-Step Setup Instructions

### 1️⃣ Clone the Repository

```powershell
# Choose your installation directory
cd D:\
git clone <your-repo-url> MEIC-main
cd MEIC-main
```

---

### 2️⃣ Create Python Virtual Environment

```powershell
# Create virtual environment
python -m venv .venv

# Activate it
.venv\Scripts\Activate.ps1

# If you get execution policy error, run:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

---

### 3️⃣ Install Python Dependencies

```powershell
pip install -r requirements.txt
```

**Required packages:**
- requests
- rauth
- paho-mqtt (version 2.1.0)
- openpyxl
- pandas
- schwab-py
- python-dotenv
- flask
- flask-socketio
- pandas-market-calendars
- beautifulsoup4

---

### 4️⃣ Install Mosquitto MQTT Broker

```powershell
# Install using winget
winget install EclipseFoundation.Mosquitto

# Start the service
Start-Service -Name mosquitto

# Verify it's running
Get-Service -Name mosquitto
# Should show: Status: Running
```

---

### 5️⃣ Configure Schwab API Credentials

**File to edit:** `common/auth/config.py`

```python
CLIENT_ID = "QEihsG3zJ1k4Two5RwZ5ltBytUPg19oV"  # Your Schwab App Key
CLIENT_SECRET = "<your_app_secret_here>"          # Your Schwab App Secret
P_ACCT = "44952712"                                # Your 10-digit account number
```

**IMPORTANT:** Your Schwab Developer App must have this callback URL configured:
```
https://127.0.0.1:8080/callback
```

If you need to create a Schwab Developer App:
1. Go to https://developer.schwab.com
2. Create a new App
3. Set Callback URL to: `https://127.0.0.1:8080/callback`
4. Wait for approval (24-48 hours)
5. Get your App Key (CLIENT_ID) and App Secret (CLIENT_SECRET)

---

### 6️⃣ Generate Schwab Auth Token

```powershell
cd common\auth
python generate_token.py
```

**Interactive process:**
1. Script prints a URL — **open it in your browser**
2. Log in to Schwab, select your account, accept the agreement
3. Browser redirects to `https://127.0.0.1:8080/callback?code=...` (will show connection error — that's normal)
4. **Copy the ENTIRE URL** from the address bar
5. **Paste it into the terminal** and press Enter
6. Script creates `common/auth/token.json` — your live token

**Note:** The token auto-refreshes every 25 minutes when running. If you restart after 7 days, you'll need to regenerate it.

---

### 7️⃣ Verify Directory Structure

Make sure these directories exist:
```
meic0dte/logs/          # Trading logs
dashboard/              # Dashboard server
streaming/              # WebSocket streamer
common/auth/            # Token storage
```

The `meic0dte/logs/` folder should exist. If not, create it:
```powershell
mkdir meic0dte\logs
```

---

### 8️⃣ Configuration Files to Review

**Trading quantity** — `meic0dte/app/config.py`:
```python
QUANTITY = 1  # Number of contracts per spread (currently set to 1 for safety)
```

**Trading schedule** — `run.py`:
```python
TRANCHES = [
    (10, 59),  # 11:00 AM lot
    (11, 59),  # 12:00 PM lot
    (12, 29),  # 12:30 PM lot
    (13, 14),  # 01:15 PM lot
    (13, 44),  # 01:45 PM lot
    (13, 59),  # 02:00 PM lot
]
```

**Times are in Central Time (CT)** — adjust if server is in different timezone.

---

### 9️⃣ Test Run (DRY RUN FIRST)

**Before running live, test in paper/demo mode if possible**

```powershell
# From project root
python run.py
```

**What happens:**
- **8:30 AM CT** — Streaming publisher starts
- **10:59 AM CT** — First tranche opens
- **Continues** through remaining tranches
- **3:00 PM CT** — Everything stops automatically

**Dashboard URL:**
```
http://localhost:5001
```

**Check logs:**
- `launcher.log` — Main launcher activity
- `streaming/stream_pub.log` — WebSocket streaming
- `meic0dte/logs/` — Individual tranche logs

---

## 🔄 Daily Operation

### Morning Startup (9:00 AM CT or earlier)

```powershell
cd D:\MEIC-main
.venv\Scripts\Activate.ps1
python run.py
```

**That's it!** The bot:
- Checks if today is a trading day (skips weekends, holidays, FOMC days)
- Starts the streaming publisher at 8:30 AM
- Opens each tranche at scheduled times
- Auto-refreshes Schwab token every 25 minutes
- Stops everything at 3:00 PM

---

## 📊 Monitor the Dashboard

Open in any browser:
```
http://localhost:5001
```

Shows:
- Live P&L
- Bot status (Idle / Opening / Closing)
- Trade details for each tranche
- Real-time logs

---

## 🛑 Emergency Kill Switch

If you need to stop the bot immediately:

```powershell
python meic0dte\meickillswitch.py
```

This closes all open positions and stops all processes.

---

## 🔧 Advanced Configuration

### Notification Alerts
Currently configured for **Telegram** (not Slack).
Slack webhook URLs in the code are empty and silently skip.

**TODO:** Add Telegram bot token + chat_id if you want alerts.

**Files with notification config:**
- `streaming/slack_alert.py`
- `meic0dte/slack/slack_alert.py`
- `common/auth/slack_alert.py`

### Holiday & FOMC Calendar
Auto-updated from NYSE and Federal Reserve website.

**File:** `common/config.py`
- `nyse_holidays()` — NYSE closed days
- `fomc_meeting_dates()` — FOMC dates scraped from Fed site
- Bot auto-skips FOMC day + day after

Cache file: `common/fomc_cache.json` (auto-refreshes if older than 30 days)

---

## 📁 Important Files Reference

| File | Purpose |
|------|---------|
| `run.py` | **Main launcher** — start this daily |
| `dashboard/server.py` | Web dashboard (auto-launched by run.py) |
| `streaming/publish.py` | Schwab WebSocket → MQTT (auto-launched) |
| `meic0dte/app_main.py` | Trading engine (runs per tranche) |
| `common/auth/token.json` | **Your live auth token** (auto-refreshed) |
| `common/auth/generate_token.py` | Token regeneration script |
| `meic0dte/meickillswitch.py` | Emergency stop |
| `launcher.log` | Main system log |

---

## 🐛 Troubleshooting

### "ModuleNotFoundError"
Make sure you activated the virtual environment:
```powershell
.venv\Scripts\Activate.ps1
```

### "Mosquitto connection failed"
Check service is running:
```powershell
Get-Service -Name mosquitto
# If stopped:
Start-Service -Name mosquitto
```

### "Token expired"
Regenerate token:
```powershell
cd common\auth
python generate_token.py
```

### "No trades executing"
Check:
1. Is today a trading day? (Not weekend/holiday/FOMC)
2. Are you within trading hours? (10:59 AM - 2:00 PM CT)
3. Check `launcher.log` for errors
4. Check VIX levels in dashboard (bot may skip if VIX too low/high)

---

## ✅ Pre-Launch Checklist

- [ ] Python 3.10+ installed
- [ ] Virtual environment created and activated
- [ ] All dependencies installed (`pip install -r requirements.txt`)
- [ ] Mosquitto MQTT broker installed and running
- [ ] Schwab credentials configured in `common/auth/config.py`
- [ ] Auth token generated (`common/auth/token.json` exists)
- [ ] `meic0dte/logs/` directory exists
- [ ] Dashboard accessible at http://localhost:5001
- [ ] Test run completed successfully
- [ ] Current date is a trading day (not weekend/holiday/FOMC)

---

## 🎯 First Day Startup Commands

```powershell
# 1. Navigate to project
cd D:\MEIC-main

# 2. Activate virtual environment
.venv\Scripts\Activate.ps1

# 3. Start the bot
python run.py
```

**Open dashboard in browser:**
```
http://localhost:5001
```

**Monitor logs:**
```powershell
# Main log
Get-Content launcher.log -Wait

# Streaming log  
Get-Content streaming\stream_pub.log -Wait
```

---

## 📞 Support Notes

- Project uses **Python 3.13** (tested and working)
- Timezone: **Central Time (CT)** — all times in code are CT
- Current trading quantity: **1 contract** (set in `meic0dte/app/config.py`)
- Account number: **44952712**
- Schwab Client ID: **QEihsG3zJ1k4Two5RwZ5ltBytUPg19oV**

---

## 🔐 Security Reminders

1. **Never commit** `common/auth/token.json` to Git
2. **Never commit** `.env` files with credentials
3. Keep `CLIENT_SECRET` secure
4. Dashboard runs on localhost — use VPN if accessing remotely
5. Consider firewall rules for port 5001 if exposing dashboard

---

## 📝 Change Log

See `CHANGES-pavi.md` for detailed modification history.

---

**That's it! You're ready to deploy.** 🚀

Run `python run.py` tomorrow morning and let the bot trade automatically.
