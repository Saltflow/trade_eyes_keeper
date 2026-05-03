#!/bin/bash
# ================================================================
# Stock Quant System — 服务器一键初始化脚本
# ================================================================
# 1. 登录你的云服务器:  ssh root@你的IP
# 2. 把此脚本上传到服务器:  scp scripts/server_init.sh root@IP:/tmp/
# 3. 在服务器上执行:  bash /tmp/server_init.sh
# ================================================================
set -e

echo "=============================================="
echo " Stock Quant System — 服务器初始化"
echo "=============================================="

# ── 1. 系统依赖 ──
echo ""
echo "[1/5] 安装系统依赖..."
apt update -qq
apt install -y -qq python3 python3-pip python3-venv \
   texlive-xetex texlive-latex-recommended texlive-latex-extra \
   poppler-utils screen curl 2>/dev/null

echo "  ✅ python3: $(python3 --version)"
echo "  ✅ xelatex: $(xelatex --version 2>&1 | head -1 || echo 'installed')"

# ── 2. 创建项目目录并初始化 Git 仓库 ──
echo ""
echo "[2/5] 创建项目目录..."
PROJECT_DIR="/root/trade_eyes_keeper"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

if [ ! -d .git ]; then
    git init
    git config receive.denyCurrentBranch updateInstead
    git config user.email "deploy@trade-eyes-keeper"
    git config user.name "Deploy Bot"
    # 创建一个空的初始提交，避免 push 时报 "unborn HEAD" 错误
    git commit --allow-empty -m "init: server repo ready for deploy"
    echo "  ✅ Git 仓库已初始化 (含空初始提交)"
else
    # 已有仓库: 确保配置正确
    git config receive.denyCurrentBranch updateInstead
    echo "  ✅ Git 仓库已存在"
fi

# ── 3. 创建配置文件模板 (首次需要) ──
echo ""
echo "[3/5] 检查配置文件..."
if [ ! -f config/config.yaml ]; then
    echo ""
    echo "  ⚠️  config/config.yaml 不存在。"
    echo "  请从本地项目复制 config/config.yaml.example 并修改:"
    echo "    scp config/config.yaml root@IP:$PROJECT_DIR/config/"
    echo "    ssh root@IP 'cd $PROJECT_DIR && vim config/config.yaml'"
    echo ""
fi

if [ ! -f config/.env ]; then
    echo ""
    echo "  ⚠️  config/.env 不存在。"
    echo "  请从本地项目复制 config/.env.example 并修改:"
    echo "    scp config/.env root@IP:$PROJECT_DIR/config/"
    echo ""
fi

# ── 4. 安装 Python 依赖 ──
echo ""
echo "[4/5] 安装 Python 依赖..."
if [ -f requirements.txt ]; then
    pip3 install --quiet -r requirements.txt 2>/dev/null || \
       python3 -m pip install --quiet -r requirements.txt 2>/dev/null && \
       echo "  ✅ Python 依赖安装完成"
else
    echo "  ⚠️  requirements.txt 未找到 (首次部署后会自动出现)"
fi

# ── 5. 配置 crontab ──
echo ""
echo "[5/5] 配置定时任务 (首次部署后生效)..."
# 不在这里配置 cron，由 ci_cd_deploy.py 自动管理
echo "  ℹ️  cron 任务将在首次 ci_cd_deploy.py 后自动配置"

echo ""
echo "=============================================="
echo " ✅ 服务器初始化完成！"
echo "=============================================="
echo ""
echo "  下一步 — 在你的本地电脑执行:"
echo "    1. 编辑 config/.env，填入 DEPLOY_HOST=你的服务器IP"
echo "    2. 生成 SSH 密钥: ssh-keygen -t ed25519 -f deploy_key -N \"\""
echo "    3. 上传公钥: ssh-copy-id -i deploy_key.pub root@你的服务器IP"
echo "    4. 首次部署: python ci_cd_deploy.py"
echo ""
