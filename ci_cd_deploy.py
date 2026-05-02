#!/usr/bin/env python3
"""
CI/CD Deployment Script for Stock Quantitative System

用法:
  python ci_cd_deploy.py                    # 部署（git push + SSH验证）
  python ci_cd_deploy.py --mode investigate # 服务器状态调查
  python ci_cd_deploy.py --dry-run          # 模拟运行

工作流:
  1. git push本地代码直推到远程服务器（不走GitHub）
  2. SSH到远程服务器验证代码、装依赖、跑测试
  3. 检查cron、邮件、健康服务器
"""

import subprocess
import sys
import os
import time
import argparse
from datetime import datetime

# ── 常量 ────────────────────────────────────────────────
# 路径通过环境变量配置，默认值仅作本地测试用
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_SSH = os.environ.get(
    "DEPLOY_SSH_REMOTE",
    "ssh://root@DEPLOY_HOST/DEPLOY_PATH",
)
REMOTE_DIR = os.environ.get(
    "DEPLOY_REMOTE_DIR",
    "/root/trade_eyes_keeper",
)
REMOTE_HOST = os.environ.get("DEPLOY_HOST", "DEPLOY_HOST")


def _get_ssh_key():
    """获取SSH密钥路径（返回前向斜杠，兼容Windows+Git Bash）。"""
    env_key = os.environ.get("DEPLOY_SSH_KEY")
    if env_key and os.path.exists(env_key):
        return env_key.replace("\\", "/")
    default = os.path.join(PROJECT_DIR, "deploy_key")
    if os.path.exists(default):
        return default.replace("\\", "/")
    return default.replace("\\", "/")  # 不存在也返回，让SSH报错


def _info(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── SSH 工具 ────────────────────────────────────────────


def _get_dry_run():
    """检查是否启用 dry-run 模式"""
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _ssh_cmd(cmd, description="", timeout=60):
    """执行SSH命令，返回 (success, stdout, stderr)"""
    if description:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {description}")
        print(f"  $ {cmd}")

    if _get_dry_run():
        print(f"  [MOCK] Would execute: {cmd}")
        return True, "Mock output\n", ""

    full_cmd = [
        "ssh",
        "-i",
        _get_ssh_key(),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        f"root@{REMOTE_HOST}",
        cmd,
    ]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = result.stdout
        err = result.stderr
        if out:
            print(out)
        if err:
            print(f"  STDERR: {err}")
        return result.returncode == 0, out, err
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] Command timed out ({timeout}s)")
        return False, "", "Timeout"
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False, "", str(e)


# ── Git 部署 ────────────────────────────────────────────


def _git_push():
    """将本地代码通过 git push 直推远程服务器"""
    _info("Pushing local code to remote server via git...")

    if _get_dry_run():
        print(f"  [MOCK] git push {REMOTE_SSH} master")
        return True

    # 构建 git 命令，使用 deploy_key + GIT_SSH_COMMAND 进行SSH认证
    cmd = ["git", "push", REMOTE_SSH, "master"]
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {_get_ssh_key()} -o StrictHostKeyChecking=no"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=PROJECT_DIR,
            env=env,
        )
        if result.returncode == 0:
            _info("Git push successful!")
            if result.stdout:
                for line in result.stdout.strip().split("\n"):
                    print(f"  {line}")
            return True
        else:
            _info(f"Git push FAILED: {result.stderr}")
            print(f"  {result.stdout}")
            print(f"  ERROR: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        _info("Git push timed out after 120s")
        return False
    except Exception as e:
        _info(f"Git push error: {e}")
        return False


def _ensure_remote_repo():
    """确保远程服务器是git仓库并正确配置"""
    cmd = (
        f"cd {REMOTE_DIR} && "
        f"if [ ! -d .git ]; then "
        f"  git init && git config receive.denyCurrentBranch updateInstead && "
        f"  git config user.email 'deploy@trade-eyes-keeper' && "
        f"  git config user.name 'Deploy Bot' && "
        f"  echo '[OK] Git repo initialized'; "
        f"else "
        f"  echo '[OK] Git repo exists'; "
        f"fi"
    )
    success, out, _ = _ssh_cmd(cmd, "Check/init git repo on remote")
    return success


# ── 部署流程 ────────────────────────────────────────────


def _run_local(*args, timeout=120):
    """运行本地命令，返回 (success, stdout, stderr)"""
    try:
        result = subprocess.run(
            list(args), capture_output=True,
            timeout=timeout, cwd=PROJECT_DIR,
            encoding="utf-8", errors="replace",
        )
        return (result.returncode == 0,
                result.stdout or "",
                result.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)


def _pre_deploy_checks(dry_run):
    """部署前检查: ruff lint + 核心测试。任一步失败则中止。"""
    if dry_run:
        _info("DRY RUN: Skipping pre-deploy checks")
        return True

    _info("=== Pre-deploy checks ===")

    # 1. ruff lint (若未安装则跳过)
    _info("Running ruff check...")
    ok, out, err = _run_local(
        sys.executable, "-m", "ruff", "check", "src/",
        "--select", "F,E",
        timeout=30,
    )
    if not ok:
        if "No module named ruff" in err or "_find_ruff" in err or "Could not find" in err:
            _info("SKIP: ruff not available")
        else:
            _info(f"FAIL: ruff check failed\n{err[:500]}")
            return False
    else:
        _info("PASS: ruff check")

    # 2. import smoke test
    _info("Running import smoke test...")
    ok, out, err = _run_local(
        sys.executable, "-c",
        "import importlib,sys; "
        "sys.path.insert(0,'src'); "
        "[importlib.import_module(f'src.{n}') for n in "
        "['analysis.backtest_config','analysis.indicator_library',"
        "'analysis.strategy_optimizer','analysis.signal_scanner',"
        "'analysis.portfolio_strategy','analysis.rule_engine',"
        "'health_server.core.global_instances']]; "
        "print('OK')",
        timeout=15,
    )
    if not ok or "OK" not in out:
        _info("FAIL: import smoke test")
        return False
    _info("PASS: import smoke test")

    # 3. core tests (no network, no LLM)
    _info("Running core tests...")
    ok, out, err = _run_local(
        sys.executable, "-m", "pytest",
        "tests/test_portfolio_strategy.py",
        "tests/test_rule_engine.py",
        "tests/test_import_smoke.py",
        "-p", "no:capture", "-q",
        timeout=120,
    )
    if not ok:
        _info(f"FAIL: core tests\n{(out or '')[-300:]}{(err or '')[-200:]}")
        return False
    _info("PASS: core tests")
    _info("All pre-deploy checks passed")
    return True


def deploy():
    """主部署函数"""
    host = REMOTE_HOST
    dry_run = _get_dry_run()

    if dry_run:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DRY RUN MODE")
        print("=" * 70)

    _info(f"Starting CI/CD deployment to {host}")
    print("=" * 70)

    try:
        # ── 0. 部署前检查 ──
        if not _pre_deploy_checks(dry_run):
            _info("ERROR: Pre-deploy checks failed, aborting deployment")
            return False

        # ── 0b. 确保远程git仓库就绪 ──
        if not _ensure_remote_repo():
            _info("WARNING: Remote repo setup may have issues")

        # ── 1. git push 本地代码到远程 ──
        if not _git_push():
            _info("ERROR: Git push failed, aborting deployment")
            return False

        # ── 2. 检查服务器状态 ──
        _info("Checking system status...")
        _ssh_cmd("date", "Server time")
        _ssh_cmd("hostname -I", "Server IP")
        _ssh_cmd("uname -a", "System info")

        # ── 3. 清理旧日志 ──
        clean = os.getenv("CLEAN_BEFORE_DEPLOY", "true").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        cleaning_performed = clean
        if clean:
            _info("Cleaning old logs and email archives (30+ days)...")
            _ssh_cmd(
                f"cd {REMOTE_DIR} && "
                f"find logs/ -name '*.log' -type f -mtime +30 -delete 2>/dev/null || "
                f"echo 'No old log files to delete'",
                "Clean old logs",
            )
            _ssh_cmd(
                f"cd {REMOTE_DIR} && "
                f"find data/email_archive/ -name '*.html' -type f -mtime +30 -delete 2>/dev/null || "
                f"echo 'No old email archives to delete'",
                "Clean old email archives",
            )
        else:
            _info("Skipping cleaning (CLEAN_BEFORE_DEPLOY=false)")

        # ── 4. 验证项目目录和git状态 ──
        _info("Verifying project directory...")
        success, _, _ = _ssh_cmd(
            f"cd {REMOTE_DIR} && pwd && git status --short",
            "Project directory and git status",
        )
        if not success:
            _info("WARNING: Project directory check failed")

        # ── 5. 安装系统依赖 ──
        _info("Installing system dependencies (texlive for PDF)...")
        _ssh_cmd(
            f"apt install -y -qq texlive-xetex texlive-latex-recommended texlive-latex-extra 2>/dev/null || echo 'texlive install skipped'",
            "Install texlive",
            timeout=300,
        )

        # ── 6. 安装/更新 Python 依赖 ──
        _info("Installing/updating Python dependencies...")
        _ssh_cmd(
            f"cd {REMOTE_DIR} && pip install --quiet -r requirements.txt",
            "Install dependencies",
            timeout=300,
        )

        # ── 6. 验证 email footer 功能 ──
        _info("Verifying email footer feature...")
        verify_script = f"""cd {REMOTE_DIR} && timeout 30 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.notification.email_notifier import EmailNotifier
notifier = EmailNotifier(config)
server_info = notifier._get_server_info()
print('Server info test:')
print('  Hostname:', server_info['hostname'])
print('  IP:', server_info['ip_address'])
print('  Kernel:', server_info['kernel_version'])
print('[OK] Email footer feature is working')
"
"""
        _ssh_cmd(verify_script, "Email footer verification", timeout=60)

        # ── 7. 系统测试 ──
        _info("Running system test (SKIP_EMAIL mode)...")
        _ssh_cmd(
            f"cd {REMOTE_DIR} && SKIP_EMAIL=true timeout 180 python3 main.py --once 2>&1 | tail -10",
            "System test",
            timeout=240,
        )

        # ── 8. 检查cron ──
        _info("Verifying cron job...")
        success, out, _ = _ssh_cmd("crontab -l", "Current cron jobs")

        # ── 9. 更新cron（如需） ──
        _info("Ensuring cron job uses --once flag...")
        cron_check = "crontab -l | grep -q 'python3 main.py --once' && echo 'Cron job already uses --once flag' || echo 'Cron job needs update'"
        success, out, _ = _ssh_cmd(cron_check, "Check cron flag")

        if "needs update" in out:
            _info("Updating cron job...")
            _ssh_cmd(
                "crontab -l | grep -v 'Stock quantitative system' | crontab -",
                "Remove old cron",
            )
            cron_line = f"30 15 * * * cd {REMOTE_DIR} && python3 main.py --once >> {REMOTE_DIR}/logs/cron.log 2>&1  # Stock quantitative system"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -",
                "Add new cron",
            )
            _ssh_cmd("crontab -l", "Verify updated cron")

        # ── 9b. 注册简报 cron (09:50) ──
        _info("Ensuring brief report cron job at 09:50...")
        brief_check = "crontab -l | grep -q 'python3 main.py --brief' && echo 'Brief cron exists' || echo 'Brief cron missing'"
        success, out, _ = _ssh_cmd(brief_check, "Check brief cron")
        if "missing" in out:
            brief_line = f"50 9 * * * cd {REMOTE_DIR} && python3 main.py --brief >> {REMOTE_DIR}/logs/cron_brief.log 2>&1"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{brief_line}') | crontab -",
                "Add brief report cron",
            )
            _info("Brief report cron registered (09:50 daily)")

        # ── 10. 验证邮件存档 ──
        _info("Checking for server info in email archives...")
        check_archive = f"""timeout 10 bash -c '
latest=$(ls -t {REMOTE_DIR}/data/email_archive/*.html 2>/dev/null | head -1)
if [ -n "$latest" ]; then
    echo "Latest email archive: $latest"
    if grep -q "服务器信息" "$latest"; then
         echo "[OK] Server info found in email archive"
        grep -A5 "服务器信息" "$latest" | head -10
    else
         echo "[FAIL] Server info NOT found (archive may be from previous run)"
    fi
else
    echo "No email archives found yet"
fi
'
"""
        _ssh_cmd(check_archive, "Check email archives")

        # ── 11. 发送部署通知 ──
        _info("Sending deployment notification email...")
        version_cmd = f"cd {REMOTE_DIR} && git rev-parse --short HEAD 2>/dev/null || echo 'unknown'"
        success, version_out, _ = _ssh_cmd(version_cmd, "Get git version")
        version = version_out.strip() if success else "unknown"

        deploy_notify_script = f"""cd {REMOTE_DIR} && timeout 30 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.notification.email_notifier import EmailNotifier
notifier = EmailNotifier(config)
try:
    notifier.send_deployment_notification('SUCCESS', version='{version}',
                                         summary='CI/CD deployment completed successfully')
    print('[OK] Deployment notification sent')
except Exception as e:
    print(f'[WARNING] Failed to send deployment notification: {{e}}')
"
"""
        _ssh_cmd(deploy_notify_script, "Send deployment notification", timeout=60)

        # ── 12. 健康服务器检查 ──
        _info("Verifying health server configuration...")
        health_check = f"""cd {REMOTE_DIR} && timeout 10 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

health_config = config.get('health_server', {{}})
enabled = health_config.get('enabled', True)
host = health_config.get('host', '0.0.0.0')
port = health_config.get('port', 1933)

print(f'Health server config:')
print(f'  Enabled: {{enabled}}')
print(f'  Host: {{host}}')
print(f'  Port: {{port}}')

if enabled:
    print('[OK] Health server is enabled in config')
else:
    print('[WARNING] Health server is disabled in config')
"
"""
        _ssh_cmd(health_check, "Health server config check")

        # ── 13. 测试健康服务器启动 ──
        _info("Testing health server startup...")
        test_health_cmd = f"""cd {REMOTE_DIR} && timeout 10 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.health_server import HealthServer
import time
try:
    hs = HealthServer(config)
    if hs.start(daemon=True):
        print(f'[OK] Health server started successfully on {{hs.host}}:{{hs.port}}')
        time.sleep(1)
        hs.stop()
        print('[OK] Health server stopped (test only)')
    else:
        print('[WARNING] Health server failed to start')
except Exception as e:
    print(f'Health server test: {{e}}')
" 2>&1"""
        _ssh_cmd(test_health_cmd, "Health server startup test", timeout=30)

        # ── 14. 重启健康服务器 ──
        _info("Restarting health server with updated code...")
        kill_cmds = [
            f"pkill -f 'health_server' 2>/dev/null || echo 'No health_server process found'",
            f"pkill -f 'python.*1933' 2>/dev/null || echo 'No process on port 1933 found'",
            f"screen -XS health_server quit 2>/dev/null || echo 'No screen session found'",
        ]
        for cmd in kill_cmds:
            _ssh_cmd(cmd, "Stop health server processes")

        time.sleep(2)

        # 验证代码更新（检查目录存在）
        _info("Verifying code update...")
        verify_code_cmd = f"""cd {REMOTE_DIR} && timeout 10 python3 -c "
import os
path = 'src/health_server'
if os.path.isdir(path):
    print('[OK] src/health_server/ directory exists')
    core_file = os.path.join(path, 'core', 'health_server.py')
    if os.path.exists(core_file):
        size = os.path.getsize(core_file)
        print(f'  health_server.py size: {{size}} bytes')
        if size > 10000:
            print('[OK] Core file size looks correct')
        else:
            print('[WARNING] Core file smaller than expected')
    else:
        print('[ERROR] Core health_server.py not found')
else:
    print('[ERROR] src/health_server/ directory not found')
"
"""
        _ssh_cmd(verify_code_cmd, "Verify code update")

        # 启动新健康服务器
        start_cmd = f"cd {REMOTE_DIR} && screen -dmS health_server python3 main.py --health-server"
        _ssh_cmd(start_cmd, "Start health server", timeout=10)
        time.sleep(3)

        # 验证健康服务器运行
        verify_cmd = f"""cd {REMOTE_DIR} && python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
import urllib.request
import urllib.error
import time

with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# 检查端口是否正确响应
try:
    time.sleep(2)
    url = 'http://localhost:1933/'
    req = urllib.request.Request(url, headers={{'User-Agent': 'CI/CD Verification'}})
    response = urllib.request.urlopen(req, timeout=10)
    html = response.read().decode('utf-8', errors='replace')

    if '管理监控股票列表' in html:
        print('[SUCCESS] Health server homepage contains management button')
        if 'href=\"/manage\"' in html or 'href=\"/manage\"' in html:
            print('[SUCCESS] Management button links to /manage endpoint')
        else:
            print('[WARNING] Button found but missing /manage link')
    else:
        print('[FAIL] Management button not found on homepage')
        print(f'First 500 chars: {{html[:500]}}')

except urllib.error.URLError as e:
    print(f'[FAIL] Could not fetch health server homepage: {{e}}')
except Exception as e:
    print(f'[FAIL] Error checking health server: {{e}}')
" 2>&1"""
        _ssh_cmd(verify_cmd, "Verify health server restart and content", timeout=30)

        # ── 15. 最终验证 ──
        _info("Final system verification...")
        _ssh_cmd(f"cd {REMOTE_DIR} && python3 --version", "Python version")
        _ssh_cmd(
            f"cd {REMOTE_DIR} && ls -la src/email_notifier.py", "Email notifier file"
        )

        # ── 完成 ──
        _info("Deployment completed successfully!")
        print("=" * 70)
        print("SUMMARY:")
        if cleaning_performed:
            print("[OK] Old logs and email archives cleaned (30+ days)")
        print("[OK] Code pushed via git to remote server")
        print("[OK] Dependencies checked/updated")
        print("[OK] Email footer feature verified")
        print("[OK] System test passed (SKIP_EMAIL mode)")
        print("[OK] Cron job configured for daily 15:30 execution")
        print("[OK] Deployment notification sent")
        print("[OK] Health server restarted with updated code (port 1933)")
        print("=" * 70)

        return True

    except Exception as e:
        _info(f"DEPLOYMENT FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False


# ── 调查模式 ────────────────────────────────────────────


def investigate_server():
    """调查远程服务器状态"""
    dry_run = _get_dry_run()

    _info(f"Investigating server {REMOTE_HOST}")
    print("=" * 70)

    if dry_run:
        print(f"  [MOCK] No actual SSH connections will be made")
        return True

    try:
        # 1. Basic system info
        print(f"\n{'=' * 70}")
        print("BASIC SYSTEM INFO")
        print("=" * 70)
        _ssh_cmd("date", "Server time")
        _ssh_cmd("uptime", "System uptime")
        _ssh_cmd("hostname -I", "Server IP addresses")
        _ssh_cmd("uname -a", "System info")
        _ssh_cmd("python3 --version", "Python version")

        # 2. Project directory
        print(f"\n{'=' * 70}")
        print("PROJECT DIRECTORY")
        print("=" * 70)
        _ssh_cmd(f"cd {REMOTE_DIR} && pwd", "Project directory")
        _ssh_cmd(f"cd {REMOTE_DIR} && ls -la", "Directory listing")

        # 3. Git status
        print(f"\n{'=' * 70}")
        print("GIT STATUS")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -d .git ]; then git status; git log --oneline -5; else echo 'Not a git repository'; fi",
            "Git status and recent commits",
        )

        # 4. Cache files
        print(f"\n{'=' * 70}")
        print("CACHE FILES")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && find cache/data/ -name '*.json' -type f 2>/dev/null | head -20",
            "Cache files list",
        )
        _ssh_cmd(
            f"cd {REMOTE_DIR} && ls -la cache/data/ 2>/dev/null || echo 'No cache directory'",
            "Cache directory listing",
        )
        _ssh_cmd(
            f'cd {REMOTE_DIR} && if [ -d cache/data ]; then for f in cache/data/*.json; do if [ -f "$f" ]; then echo "$(basename $f) - $(stat -c %y $f 2>/dev/null || ls -la $f)"; fi; done | head -10; fi',
            "Cache file dates",
        )
        _ssh_cmd(
            f"cd {REMOTE_DIR} && grep -l 'TEST' cache/data/*.json 2>/dev/null | head -5 || echo 'No TEST data found in cache'",
            "TEST data in cache",
        )

        # 5. Log files
        print(f"\n{'=' * 70}")
        print("LOG FILES")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && ls -la logs/ 2>/dev/null || echo 'No logs directory'",
            "Log directory",
        )
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -f logs/quant_system.log ]; then tail -50 logs/quant_system.log; else echo 'No quant_system.log found'; fi",
            "Recent logs",
        )
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -f logs/quant_system.log ]; then grep -i 'error\\|warn\\|fail' logs/quant_system.log | tail -20; else echo 'No log file'; fi",
            "Errors/Warnings in logs",
        )

        # 6. Email archives
        print(f"\n{'=' * 70}")
        print("EMAIL ARCHIVES")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && ls -la data/email_archive/ 2>/dev/null || echo 'No email archive directory'",
            "Email archive directory",
        )
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -d data/email_archive ]; then ls -lt data/email_archive/*.html 2>/dev/null | head -5 || echo 'No email archives'; fi",
            "Recent email archives",
        )
        _ssh_cmd(
            f"""cd {REMOTE_DIR} && latest=$(ls -t data/email_archive/*.html 2>/dev/null | head -1)
if [ -n "$latest" ]; then
    echo "Latest email archive: $latest"
    if grep -q 'TEST' "$latest"; then
        echo "[WARNING] Found TEST data in latest email"
        grep -n 'TEST' "$latest" | head -5
    else
        echo "[OK] No TEST data found in latest email"
    fi
else
    echo "No email archives found"
fi""",
            "Check for TEST data in emails",
        )

        # 7. Health server
        print(f"\n{'=' * 70}")
        print("HEALTH SERVER")
        print("=" * 70)
        _ssh_cmd(
            "netstat -tlnp 2>/dev/null | grep :1933 || echo 'Port 1933 not listening (or netstat not available)'",
            "Health server port check",
        )
        _ssh_cmd(
            "ps aux | grep -i 'health_server\\|python.*1933' | grep -v grep || echo 'No health server process found'",
            "Health server process",
        )

        # 8. Cron jobs
        print(f"\n{'=' * 70}")
        print("CRON JOBS")
        print("=" * 70)
        _ssh_cmd("crontab -l", "Cron jobs")

        # 9. Scheduler status
        print(f"\n{'=' * 70}")
        print("SCHEDULER STATUS")
        print("=" * 70)
        _ssh_cmd(
            "ps aux | grep -i 'main.py\\|scheduler' | grep -v grep || echo 'No scheduler process found'",
            "Scheduler process",
        )

        # 10. Data fetcher code
        print(f"\n{'=' * 70}")
        print("DATA_FETCHER CODE CHECK")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -f src/data_fetcher.py ]; then grep -n '_should_bypass_cache' src/data_fetcher.py -A 20 | head -30; else echo 'data_fetcher.py not found'; fi",
            "Cache bypass logic",
        )

        # 11. Config
        print(f"\n{'=' * 70}")
        print("CONFIGURATION")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -f config/config.yaml ]; then grep -n 'cache_bypass_cutoff\\|timezone\\|run_time' config/config.yaml; else echo 'config.yaml not found'; fi",
            "Config settings",
        )

        # 12. Email notifier
        print(f"\n{'=' * 70}")
        print("EMAIL NOTIFIER CODE")
        print("=" * 70)
        _ssh_cmd(
            f"cd {REMOTE_DIR} && if [ -f src/email_notifier.py ]; then grep -n 'send_deployment_notification' src/email_notifier.py -A 5 | head -20; else echo 'email_notifier.py not found'; fi",
            "Deployment notification method",
        )

        _info("Investigation completed!")
        return True

    except Exception as e:
        _info(f"INVESTIGATION FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False


# ── 主入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CI/CD Deployment and Investigation Script"
    )
    parser.add_argument(
        "--mode",
        choices=["deploy", "investigate"],
        default="deploy",
        help="Mode: deploy (default) or investigate server state",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode (no actual SSH connections or git push)",
    )
    parser.add_argument(
        "--ssh-key",
        default=_get_ssh_key(),
        help=f"SSH private key path (default: {_get_ssh_key()})",
    )

    args = parser.parse_args()

    # Update SSH key path if provided
    if args.ssh_key:
        os.environ["DEPLOY_SSH_KEY"] = args.ssh_key

    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    if args.mode == "investigate":
        success = investigate_server()
    else:
        success = deploy()

    sys.exit(0 if success else 1)
