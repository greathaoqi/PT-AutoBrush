#!/usr/bin/env bash
# ==============================================================================
# PT-AutoBrush 一键自动化部署脚本
# 针对: Oracle Linux 8 / RHEL 8 (el8uek.aarch64 / ARM64)
# 功能: 自动安装 aarch64 静态版 qbittorrent-nox + PT-AutoBrush 核心守护与 WebUI + Systemd 服务
# ==============================================================================

set -e

# Preserve authentication and service isolation in existing deployments.
if systemctl is-active --quiet pt-brush || [ -f /etc/pt-brush/security.json ]; then
    echo '检测到已有部署。请使用 deploy_update.sh 更新，或 deploy/apply-secure.sh 迁移安全配置。' >&2
    exit 1
fi

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
NC="\033[0m"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $1"; }

# 1. 权限与架构检测
if [[ $EUID -ne 0 ]]; then
   log_err "请使用 root 权限运行本脚本 (例如: sudo bash install.sh)"
   exit 1
fi

ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
    log_warn "检测到当前 CPU 架构为 $ARCH (非 aarch64)，本脚本专为 aarch64 调优，将尝试继续..."
else
    log_info "硬件架构验证通过: $ARCH (ARM64)"
fi

INSTALL_DIR="/opt/pt-brush"
DOWNLOAD_DIR="/root/Downloads"
mkdir -p "$INSTALL_DIR"
mkdir -p "$DOWNLOAD_DIR"

# 2. 安装系统依赖 (优先适配 Oracle Linux 8 / EL8 的 Python 3.9 或 3.8)
log_info "正在配置系统依赖环境 (检测 Python 3.8+)..."
PYTHON_CMD="python3"
if command -v dnf &>/dev/null; then
    # 针对 Oracle Linux 8 优先启用并安装 Python 3.9 或 3.8，防止默认 3.6 不支持新版 FastAPI
    if dnf module enable -y python39 &>/dev/null && dnf install -y python39 python39-pip python39-devel &>/dev/null; then
        PYTHON_CMD="python3.9"
        log_info "已配置 Python 3.9 运行环境"
    elif dnf module enable -y python38 &>/dev/null && dnf install -y python38 python38-pip python38-devel &>/dev/null; then
        PYTHON_CMD="python3.8"
        log_info "已配置 Python 3.8 运行环境"
    else
        dnf install -y curl tar ca-certificates python3 python3-pip python3-devel gcc
    fi
    dnf install -y curl tar ca-certificates gcc &>/dev/null || true
elif command -v yum &>/dev/null; then
    yum install -y curl tar ca-certificates python3 python3-pip python3-devel gcc
elif command -v apt-get &>/dev/null; then
    apt-get update && apt-get install -y curl tar ca-certificates python3 python3-pip python3-venv
fi

# 3. 部署 qbittorrent-nox (aarch64 静态编译版)
QB_BIN="/usr/local/bin/qbittorrent-nox"
if [ ! -f "$QB_BIN" ]; then
    log_info "正在下载适用于 aarch64 的 qbittorrent-nox 静态二进制程序..."
    QB_URL="https://github.com/userdocs/qbittorrent-nox-static/releases/latest/download/aarch64-qbittorrent-nox"
    
    # 尝试直接下载或通过镜像加速
    if ! curl -fsSL -o "$QB_BIN" "$QB_URL"; then
        log_warn "GitHub 直接下载失败，尝试使用加速源下载..."
        curl -fsSL -o "$QB_BIN" "https://ghproxy.net/$QB_URL" || {
            log_err "下载 qbittorrent-nox 失败，请检查 VPS 网络连接！"
            exit 1
        }
    fi
    chmod +x "$QB_BIN"
    log_info "qbittorrent-nox aarch64 安装成功: $($QB_BIN --version 2>&1 | head -n 1)"
else
    log_info "检测到 $QB_BIN 已存在，跳过重新下载。"
fi

# 4. 配置 qBittorrent 初始环境 (免密本地访问与端口)
systemctl stop qbittorrent-nox 2>/dev/null || true
QB_CONF_DIR="/root/.config/qBittorrent"
mkdir -p "$QB_CONF_DIR"
cat <<EOF > "$QB_CONF_DIR/qBittorrent.conf"
[LegalNotice]
Accepted=true

[BitTorrent]
Session\Port=12345
Session\QueueingSystemEnabled=false

[Preferences]
WebUI\Port=12301
WebUI\LocalHostAuth=false
WebUI\AuthSubnetWhitelistEnabled=true
WebUI\AuthSubnetWhitelist=127.0.0.1/32
WebUI\Username=admin
Downloads\SavePath=$DOWNLOAD_DIR
EOF

# 5. 配置并启动 qbittorrent-nox 的 Systemd 服务 (使用 --webui-port=12301 命令行强约束)
log_info "正在注册 qbittorrent-nox Systemd 服务..."
cat <<EOF > /etc/systemd/system/qbittorrent-nox.service
[Unit]
Description=qBittorrent-nox Seeding Daemon
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/qbittorrent-nox --webui-port=12301
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now qbittorrent-nox
log_info "qbittorrent-nox 服务已启动并在后台运行！"

# 6. 部署 PT-AutoBrush 核心服务
log_info "正在配置 PT-AutoBrush Python 虚拟环境..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 复制脚本文件至安装目录
cp -f "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"
cp -f "$SCRIPT_DIR/metrics.py" "$INSTALL_DIR/metrics.py"
cp -f "$SCRIPT_DIR/account_status.py" "$INSTALL_DIR/account_status.py"
cp -f "$SCRIPT_DIR/index.html" "$INSTALL_DIR/index.html"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chmod 755 "$INSTALL_DIR/data"

# 【配置持久化保护】若已存在数据库或配置，坚决不覆盖
if [ ! -f "$INSTALL_DIR/data/config.db" ] && [ ! -f "$INSTALL_DIR/config.json" ]; then
    if [ -f "$SCRIPT_DIR/config.json" ]; then
        cp -f "$SCRIPT_DIR/config.json" "$INSTALL_DIR/config.json"
    fi
fi

# 创建并配置 Python 独立虚拟环境
cd "$INSTALL_DIR"
"$PYTHON_CMD" -m venv venv
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install fastapi uvicorn requests feedparser beautifulsoup4 python-multipart -q

# 7. 配置并启动 PT-AutoBrush 的 Systemd 服务
log_info "正在注册 PT-AutoBrush Systemd 守护服务..."
cat <<EOF > /etc/systemd/system/pt-brush.service
[Unit]
Description=PT-AutoBrush WebUI Service
After=network.target qbittorrent-nox.service
Wants=qbittorrent-nox.service

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/server.py
Restart=always
RestartSec=5s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now pt-brush
log_info "PT-AutoBrush 核心服务已成功启动！"

# 8. 防火墙端口放行 (兼容 firewalld 与 Oracle Linux 底层 iptables)
log_info "正在配置防火墙规则放行端口 12300 ~ 12400..."
if systemctl is-active --quiet firewalld; then
    firewall-cmd --add-port=12300/tcp --permanent &>/dev/null || true
    firewall-cmd --add-port=12301/tcp --permanent &>/dev/null || true
    firewall-cmd --add-port=12345/tcp --permanent &>/dev/null || true
    firewall-cmd --add-port=12345/udp --permanent &>/dev/null || true
    firewall-cmd --reload &>/dev/null || true
fi

# Oracle Linux 8 UEK 底层 iptables 放行 (核心：Oracle 镜像默认 REJECT 所有非 22 端口)
if command -v iptables &>/dev/null; then
    iptables -I INPUT -p tcp --dport 12300:12400 -j ACCEPT 2>/dev/null || true
    iptables -I INPUT -p udp --dport 12300:12400 -j ACCEPT 2>/dev/null || true
    service iptables save 2>/dev/null || iptables-save > /etc/sysconfig/iptables 2>/dev/null || true
fi

# 获取外网 IP
SERVER_IP=$(curl -s4 https://api.ipify.org || curl -s4 https://ifconfig.me || echo "YOUR-SERVER-IP")

# 9. 部署完成提示
echo -e "\n=================================================================="
echo -e "${GREEN}🎉 PT-AutoBrush 自动化刷流服务已全部部署完成！${NC}"
echo -e "=================================================================="
echo -e "${BLUE}▶ 刷流控制台 WebUI 地址:${NC} http://${SERVER_IP}:12300"
echo -e "${BLUE}▶ qBittorrent WebUI 地址:${NC} http://${SERVER_IP}:12301 (默认账号: admin / 初始可免密本地回环)"
echo -e "${BLUE}▶ BT Peer 传入监听端口:${NC} 12345 (TCP/UDP 已规划入 12300-12400)"
echo -e "${BLUE}▶ 种子下载保存路径:${NC} $DOWNLOAD_DIR"
echo -e ""
echo -e "${YELLOW}【Systemd 服务管理命令】:${NC}"
echo -e "  查看刷流服务状态:   systemctl status pt-brush"
echo -e "  实时查看运行日志:   journalctl -u pt-brush -f"
echo -e "  重启刷流服务:       systemctl restart pt-brush"
echo -e "  查看 qBittorrent:   systemctl status qbittorrent-nox"
echo -e "==================================================================\n"
