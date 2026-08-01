# VPS Dashboard Access — Domain, HTTPS, Google Login

**Date**: Jun 23, 2026  
**Status**: Operator guide — not implemented in repo yet (nginx/oauth2-proxy run on the VPS)  
**Audience**: Running MEIC on a Google Cloud (or other) VPS with remote dashboard access  
**Related**: [README.md](../README.md), [DISCORD_MEIC_SANG.md](DISCORD_MEIC_SANG.md), [OPERATIONAL_HARDENING.md](OPERATIONAL_HARDENING.md), [MANUAL_STRATEGY.md](MANUAL_STRATEGY.md)

---

## Purpose

You want to:

1. Run `run.py` on a VPS **in the background** (MEIC tranches + streamer + stop_monitor on schedule).
2. Open the dashboard from **phone or laptop** without being on the same LAN.
3. **Not** leave Kill / Place / Pause buttons open to the whole internet without a login.
4. Prefer **Google sign-in** restricted to **your account only**.

This document covers **cost**, **architecture**, and **step-by-step setup** using:

- A cheap domain (e.g. Namecheap **`.space`** promo ~$1.18 first year)
- **nginx** (free reverse proxy + HTTPS)
- **Let’s Encrypt** (free SSL certificate)
- **oauth2-proxy** (free Google OAuth gate — optional but recommended)

---

## Cost summary

| Item | Typical cost |
|------|----------------|
| **nginx** | Free (open source) |
| **Let’s Encrypt (certbot)** | Free |
| **Google OAuth** | Free for personal use |
| **oauth2-proxy** | Free |
| **GCP VM** | Already paying (free tier or ~$5–15/mo depending on machine) |
| **Domain (`.space` promo)** | ~**$1–2 first year** on Namecheap — check **renewal** (~$25–30/yr) before checkout |
| **Static external IP (GCP)** | Small monthly fee if you reserve one (recommended so DNS does not break) |

**No monthly “nginx fee.”** Extra ongoing cost is mostly **domain renewal** after year one.

---

## How the pieces fit (simple)

```
Your phone / laptop
    │
    │  https://meic.yourname.space   (port 443, padlock)
    ▼
┌─────────────────────────────────────────┐
│  GCP VPS                                 │
│  ┌─────────┐   ┌──────────────┐         │
│  │ nginx   │──►│ oauth2-proxy │──► Google login (your email only)
│  └────┬────┘   └──────┬───────┘         │
│       │               │                  │
│       └───────────────┼──────────────────┤
│                       ▼                  │
│              dashboard :5002 (localhost)   │
│              run.py + streamer + stop_mon  │
└─────────────────────────────────────────┘
```

- **nginx** = doorman at the front door (HTTPS).
- **oauth2-proxy** = “show Google ID; only this email gets in.”
- **dashboard** = MEIC UI (should listen on **127.0.0.1:5002** once nginx is in front — see [Bind dashboard to localhost](#6-bind-dashboard-to-localhost-optional-but-safer)).
- **Port 5002** should **not** be open to the public internet after nginx is working — only **443** (HTTPS).

---

## MEIC schedule vs Manual Spread (while VPS runs)

| Component | When it runs (Central time) |
|-----------|-----------------------------|
| **Launcher** | Wakes ~**8:20** weekdays; sleeps after **3:00 PM** until next trading day |
| **Streamer + stop_monitor** | ~**8:30 AM – 3:00 PM** |
| **MEIC tranches** | Windows from **~10:59** through **~2:00 PM** (see `run.py` `TRANCHES`) |
| **Dashboard process** | Stays up while `run.py` parent is running (including overnight sleep between sessions) |
| **Manual Spread Scan** | Anytime (REST API — no streamer required) |
| **Manual Spread Place + stops** | Best during **8:30–3:00** when streamer + stop_monitor are active |

Leave one terminal/session or **systemd** service running:

```bash
cd /path/to/MEIC-with-Dash-main
uv run python run.py
# or: python run.py
```

---

## Prerequisites

- GCP (or other) **Linux VPS** (Ubuntu 22.04/24.04 is fine)
- SSH access as a user with `sudo`
- MEIC already working on the VPS (`.env`, Mosquitto, TastyTrade auth)
- A **domain name** pointing at the VPS (see below)

---

## Step 1 — Buy and point a domain

Example: **`meic-yourname.space`** from Namecheap (~$1.18 first-year promos for `.space` are common).

**Before paying:** read the **renewal price** in the cart (often much higher than year one).

**DNS (Namecheap → Advanced DNS):**

| Type | Host | Value |
|------|------|--------|
| **A** | `@` | Your VPS **external IP** |
| **A** | `www` | Same IP (optional) |

Or use a subdomain only:

| Type | Host | Value |
|------|------|--------|
| **A** | `meic` | Your VPS external IP |

Then your URL is `https://meic.yourname.space`.

Wait 5–30 minutes for DNS to propagate. Check:

```bash
dig +short meic.yourname.space
```

---

## Step 2 — GCP firewall

In **Google Cloud Console → VPC network → Firewall**:

1. **Create ingress rule** — allow **tcp:443** from `0.0.0.0/0` (or your home IP only if you prefer).
2. **Remove or deny** public **tcp:5002** once nginx works (dashboard only on localhost).

Keep **tcp:22** (SSH) restricted to your IP if possible.

---

## Step 3 — Install nginx and certbot

SSH into the VPS:

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

---

## Step 4 — HTTPS certificate (Let’s Encrypt)

Replace with your real hostname:

```bash
sudo certbot --nginx -d meic.yourname.space
```

Follow prompts (email for expiry notices, agree to terms). Certbot edits nginx and sets auto-renewal.

Test renewal:

```bash
sudo certbot renew --dry-run
```

---

## Step 5 — nginx reverse proxy to dashboard

Edit site config (e.g. `/etc/nginx/sites-available/default` or `/etc/nginx/sites-available/meic`):

```nginx
server {
    listen 443 ssl;
    server_name meic.yourname.space;

    # ssl_certificate lines added by certbot

    location / {
        proxy_pass http://127.0.0.1:5002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

Enable and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

At this point you have **HTTPS only** — still **no login**. Anyone with the URL can use the dashboard.

---

## Step 6 — Google login (oauth2-proxy)

### 6.1 Google Cloud Console

1. [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **OAuth consent screen**  
   - User type: **External** (add yourself as **Test user** while testing).
2. **Credentials** → **Create credentials** → **OAuth client ID** → **Web application**.
3. **Authorized redirect URIs** (exact):

   ```
   https://meic.yourname.space/oauth2/callback
   ```

4. Save **Client ID** and **Client secret**.

### 6.2 Install oauth2-proxy

Download latest release from [oauth2-proxy releases](https://github.com/oauth2-proxy/oauth2-proxy/releases) or use package manager if available.

Example env file `/etc/oauth2-proxy/meic.env` (restrict to **your** Gmail):

```bash
OAUTH2_PROXY_PROVIDER=google
OAUTH2_PROXY_CLIENT_ID=your-client-id.apps.googleusercontent.com
OAUTH2_PROXY_CLIENT_SECRET=your-client-secret
OAUTH2_PROXY_REDIRECT_URL=https://meic.yourname.space/oauth2/callback
OAUTH2_PROXY_EMAIL_DOMAINS=*
OAUTH2_PROXY_AUTHENTICATED_EMAILS_FILE=/etc/oauth2-proxy/allowed_emails.txt
OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | head -c 32)
OAUTH2_PROXY_UPSTREAMS=http://127.0.0.1:5002/
OAUTH2_PROXY_HTTP_ADDRESS=127.0.0.1:4180
OAUTH2_PROXY_COOKIE_SECURE=true
OAUTH2_PROXY_SET_XAUTHREQUEST=true
```

`/etc/oauth2-proxy/allowed_emails.txt`:

```
you@gmail.com
```

Run oauth2-proxy under **systemd** (listen on `127.0.0.1:4180`).

### 6.3 Point nginx at oauth2-proxy instead of dashboard

Change nginx `proxy_pass`:

```nginx
location / {
    proxy_pass http://127.0.0.1:4180;
    # same WebSocket / header lines as above
}
```

Reload nginx. Flow: **Browser → nginx (HTTPS) → oauth2-proxy (Google) → dashboard**.

---

## Step 7 — Bind dashboard to localhost (optional but safer)

Today `dashboard/server.py` uses `host='0.0.0.0'`. Once nginx + oauth2-proxy work, change to:

```python
socketio.run(app, host='127.0.0.1', port=5002, ...)
```

Then even if someone opens port 5002 on the firewall, the app is not listening on the public interface.

**Local-only testing:** keep `0.0.0.0` or use SSH tunnel.

---

## Step 8 — Run MEIC on boot (systemd)

Example unit `/etc/systemd/system/meic.service`:

```ini
[Unit]
Description=MEIC Autotrader
After=network.target mosquitto.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/MEIC-with-Dash-main
EnvironmentFile=/home/YOUR_USER/MEIC-with-Dash-main/.env
ExecStart=/home/YOUR_USER/MEIC-with-Dash-main/.venv/bin/python run.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now meic.service
```

Separate units for **nginx** and **oauth2-proxy** (`enable` on boot).

---

## Alternatives (no domain or no public port)

| Approach | HTTPS | Google login | Public port |
|----------|-------|--------------|-------------|
| **nginx + domain + oauth2-proxy** (this doc) | Yes | Yes | 443 only |
| **SSH tunnel** `ssh -L 5002:127.0.0.1:5002 user@vps` | No (localhost) | No (unless added in app) | None |
| **Cloudflare Tunnel** | Yes (free hostname) | Via oauth2-proxy or Cloudflare Access | None on VPS |
| **Tailscale** | VPN | Optional | None public |
| **Discord bot** (see [DISCORD_MEIC_SANG.md](DISCORD_MEIC_SANG.md)) | N/A | N/A | Control without web UI |

---

## Security checklist

- [ ] HTTPS on 443; close public **5002**
- [ ] Google OAuth **allowlist** — only your email (not `@gmail.com` domain-wide)
- [ ] Change dashboard `SECRET_KEY` from default when adding sessions
- [ ] Restrict SSH to your IP
- [ ] `.env` never committed; file permissions `600`
- [ ] Paper trade on VPS before live (`python run.py --paper`)

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| certbot fails | DNS not pointing to VPS yet; wrong hostname |
| Google login loop | Redirect URI mismatch (must match exactly, including `https`) |
| Dashboard loads but no live PnL | WebSocket blocked — check nginx `Upgrade` headers |
| Scan works, stops don’t | After 3 PM streamer/stop_monitor stopped; place manual trades during session |
| 502 Bad Gateway | `run.py` not running or dashboard not on 5002 |

---

## Implementation status in this repo

| Piece | In repo? | Where it runs |
|-------|----------|----------------|
| Dashboard | Yes | `dashboard/server.py` |
| nginx config | **No** — operator installs on VPS | `/etc/nginx/` |
| oauth2-proxy config | **No** — operator installs on VPS | `/etc/oauth2-proxy/` |
| In-app Google login | **Not implemented** — oauth2-proxy preferred | — |

Future option: Flask Google OAuth inside the app (more code; must also secure Socket.IO). oauth2-proxy avoids touching trading code.

---

## Related: Discord mobile control

If you want **Kill / Pause from the phone** without opening the dashboard at all, see **[DISCORD_MEIC_SANG.md](DISCORD_MEIC_SANG.md)** — outbound alerts + inbound bot writing the same JSON sentinel files the dashboard uses. Discord complements HTTPS dashboard access; it does not replace it unless you prefer chat-only ops.

---

## Quick reference URLs

After setup:

- Dashboard: `https://meic.yourname.space`
- Google OAuth redirect: `https://meic.yourname.space/oauth2/callback`
- Local dashboard (on VPS): `http://127.0.0.1:5002`
