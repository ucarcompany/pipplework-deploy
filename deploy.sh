#!/bin/bash
# =============================================================
# Pipplework — Quick Deploy / Update Script
# Run on the server: bash deploy.sh
# =============================================================
set -e

APP_DIR="/opt/pipplework"
REPO="https://github.com/ucarcompany/pipplework-deploy.git"
BRANCH="main"

echo "===> Pulling latest code from GitHub..."
if [ -d "$APP_DIR/.deploy-repo" ]; then
    cd "$APP_DIR/.deploy-repo" && git pull --ff-only origin "$BRANCH"
else
    git clone --depth 1 -b "$BRANCH" "$REPO" "$APP_DIR/.deploy-repo"
fi

echo "===> Copying files..."
cp -r "$APP_DIR/.deploy-repo/backend/"* "$APP_DIR/backend/"
cp -r "$APP_DIR/.deploy-repo/frontend/"* "$APP_DIR/frontend/"

echo "===> Restarting service..."
systemctl restart pipplework
sleep 3
systemctl is-active pipplework

echo "===> Checking API..."
curl -s http://127.0.0.1:9800/api/status
echo ""
echo "===> Deploy complete!"
