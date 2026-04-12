#!/bin/bash
# One-command deploy for pipplework
# Run on server: curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/deploy.sh | bash
set -e
cd /opt/pipplework
echo '=== Downloading updated files ==='
curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/backend/main.py -o backend/main.py
curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/backend/crawler/printables.py -o backend/crawler/printables.py
echo '=== Files updated ==='
echo '=== Removing old DB for schema changes ==='
rm -f /opt/pipplework/data/pipeline.db
echo '=== Restarting service ==='
systemctl restart pipplework
sleep 3
echo "Service status: $(systemctl is-active pipplework)"
echo '=== Checking API ==='
curl -s http://127.0.0.1:9800/api/status
echo ''
echo '=== Checking recent logs ==='
journalctl -u pipplework -n 10 --no-pager
echo '=== Unbanning all IPs from fail2ban ==='
fail2ban-client set sshd unbanall 2>/dev/null || true
echo '=== Deploy complete ==='
