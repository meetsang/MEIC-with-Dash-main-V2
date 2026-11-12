#!/usr/bin/env bash
# ==============================================================================
# MEIC GCP e2-micro (1 GB RAM) VM Setup & Optimization Script
# ==============================================================================
# Configures a 2 GB SSD swapfile, kernel memory tuning, Mosquitto, and systemd
# services with in-process execution for running comfortably on a free-tier
# e2-micro instance without hitting OOM.
#
# Usage:
#   sudo bash deploy/setup_e2_micro.sh
# ==============================================================================

set -euo pipefail

echo "=========================================================="
echo " Starting MEIC e2-micro 1 GB RAM System Configuration..."
echo "=========================================================="

# 1. Configure 2 GB SSD Swapfile
if [ ! -f /swapfile ]; then
    echo "[1/5] Creating 2 GB swapfile at /swapfile..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "      Swapfile created and activated."
else
    echo "[1/5] Swapfile /swapfile already exists. Skipping creation."
fi

# 2. Kernel Memory Tuning for Low-RAM Trading VM
echo "[2/5] Applying kernel swappiness and cache tuning..."
sysctl -w vm.swappiness=10
sysctl -w vm.vfs_cache_pressure=50
if ! grep -q "vm.swappiness=10" /etc/sysctl.conf; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf
fi
if ! grep -q "vm.vfs_cache_pressure=50" /etc/sysctl.conf; then
    echo "vm.vfs_cache_pressure=50" >> /etc/sysctl.conf
fi

# 3. Install & Harden Local Mosquitto MQTT Broker
echo "[3/5] Installing and hardening Mosquitto MQTT broker..."
apt-get update -qq
apt-get install -y -qq mosquitto mosquitto-clients

# Restrict Mosquitto strictly to loopback (127.0.0.1)
cat << 'EOF' > /etc/mosquitto/conf.d/meic_local.conf
listener 1883 127.0.0.1
allow_anonymous true
persistence false
EOF
systemctl restart mosquitto
systemctl enable mosquitto

# 4. Install Systemd Service for MEIC In-Process Bot
echo "[4/5] Installing systemd service for MEIC bot..."
cat << 'EOF' > /etc/systemd/system/meic.service
[Unit]
Description=MEIC Autotrader (In-Process Low-Memory Mode)
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=meic
WorkingDirectory=/opt/meic
ExecStart=/opt/meic/.venv/bin/python run.py --low-memory
Restart=always
RestartSec=10
TimeoutStopSec=30
Environment="IN_PROCESS_SERVICES=true"
Environment="LOW_MEMORY_MODE=true"
Environment="MEIC_SIDE_OPTION_COLLECTION=false"
Environment="SPX_LADDER_MAX_ACTIVE_SYMBOLS=50"
Environment="LOG_RETENTION_DAYS=7"
Environment="PYTHONOPTIMIZE=1"
MemoryMax=850M

[Install]
WantedBy=multi-user.target
EOF

# 5. Install Systemd Service for MEIC Dashboard
echo "[5/5] Installing systemd service for MEIC Dashboard..."
cat << 'EOF' > /etc/systemd/system/meic-dashboard.service
[Unit]
Description=MEIC Dashboard Server (Tailscale Private Host)
After=network.target

[Service]
Type=simple
User=meic
WorkingDirectory=/opt/meic
ExecStart=/opt/meic/.venv/bin/python dashboard/server.py
Restart=always
RestartSec=5
Environment="DASHBOARD_BIND_HOST=127.0.0.1"
Environment="PYTHONOPTIMIZE=1"
MemoryMax=220M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable meic.service
systemctl enable meic-dashboard.service

echo "=========================================================="
echo " Configuration complete!"
echo " Total memory available:"
free -h
echo "=========================================================="

