#!/usr/bin/env bash
# =============================================================
# 3D Data Pipeline — Server Setup Script
# Target: Debian 12, as root
# =============================================================
set -euo pipefail

APP_DIR="/opt/pipplework"
VENV="$APP_DIR/venv"
SERVICE="pipplework"
PORT=9800

echo "===> [1/7] 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip python3-dev \
    build-essential chromium libglib2.0-0 libnss3 libx11-xcb1 \
    libxcomposite1 libxdamage1 libxi6 libxtst6 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libpango-1.0-0 libxrandr2 libxss1 \
    libasound2 fonts-liberation >/dev/null

echo "===> [2/7] 创建项目目录"
mkdir -p "$APP_DIR"/{data/{raw,cleaned,rejected},frontend/{css,js},backend/{crawler,cleaner,storage}}

echo "===> [3/7] 创建 Python 虚拟环境"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q

echo "===> [4/7] 安装 Python 依赖"
"$VENV/bin/pip" install -q \
    'fastapi==0.115.6' \
    'uvicorn[standard]==0.34.0' \
    'aiofiles==24.1.0' \
    'aiosqlite==0.20.0' \
    'curl_cffi==0.7.4' \
    'trimesh==4.5.3' \
    'numpy==2.2.1' \
    'python-multipart==0.0.20' \
    'websockets==14.1' \
    'Pillow==11.1.0' \
    'beautifulsoup4==4.12.3' \
    'lxml==5.3.0' \
    'pydantic==2.10.4' \
    'scipy==1.14.1'

echo "===> [5/7] 创建 systemd 服务"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=3D Data Pipeline Demo
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
ExecStart=${VENV}/bin/uvicorn backend.main:app --host 127.0.0.1 --port ${PORT} --workers 1
Restart=always
RestartSec=5
Environment=PIPELINE_BASE=${APP_DIR}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "===> [6/7] 检查 Chromium"
if command -v chromium &>/dev/null; then
    echo "    Chromium: $(chromium --version 2>/dev/null || echo 'installed')"
else
    echo "    ⚠ Chromium 未安装 — CDP拦截功能将不可用"
fi

echo "===> [7/7] 完成"
echo ""
echo "   项目目录: $APP_DIR"
echo "   虚拟环境: $VENV"
echo "   服务端口: $PORT"
echo "   下一步: 复制代码文件到 $APP_DIR, 然后:"
