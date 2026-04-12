#!/bin/bash
# One-command deploy for pipplework
# Run: curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/deploy.sh | bash
set -e
cd /opt/pipplework
echo '=== Downloading updated files ==='
curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/backend/main.py -o backend/main.py
curl -sL https://raw.githubusercontent.com/ucarcompany/pipplework-deploy/main/backend/crawler/printables.py -o backend/crawler/printables.py
echo '=== Files updated ==='
echo '=== Restarting service ==='
rm -f /opt/pipplework/data/pipeline.db
systemctl restart pipplework
sleep 3
systemctl is-active pipplework
echo '=== Checking API ==='
curl -s http://127.0.0.1:9800/api/status
echo ''
echo '=== Unbanning all IPs from fail2ban ==='
fail2ban-client set sshd unbanall 2>/dev/null || true
echo '=== Deploy complete ==='
