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
from dotenv import load_dotenv

# 加载 .env 配置
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", ".env"))

# ── 常量 ────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_HOST = os.environ.get("DEPLOY_HOST")
REMOTE_SSH_USER = os.environ.get("DEPLOY_SSH_USER", "root")
REMOTE_SSH = os.environ.get("DEPLOY_SSH_REMOTE")
REMOTE_DIR = os.environ.get("DEPLOY_REMOTE_DIR", "/root/trade_eyes_keeper")


def _check_prerequisites():
    """部署前自检，逐项诊断并给出修复指引。"""

    def _fail(title, items):
        print("=" * 60)
        print(f"  {title}")
        print("=" * 60)
        for i, (mark, msg) in enumerate(items, 1):
            print(f"\n  [{i}] {mark} {msg}")
        print(f"\n  修复后重新运行: python ci_cd_deploy.py\n")
        sys.exit(1)

    issues = []
    warns = []
    ssh_key = _get_ssh_key()

    # ── DEPLOY_HOST ──
    if not REMOTE_HOST:
        issues.append(("[FAIL]", "DEPLOY_HOST 未设置"))
    elif REMOTE_HOST in ("0.0.0.0", "127.0.0.1", "DEPLOY_HOST"):
        issues.append(("[FAIL]",
            f"DEPLOY_HOST = \"{REMOTE_HOST}\" — 请改成你的服务器公网 IP\n"
            "     获取方式: 登录服务器, 执行 curl ifconfig.me"))
    else:
        warns.append(("[PASS]", f"DEPLOY_HOST = {REMOTE_HOST}"))

    # ── DEPLOY_SSH_REMOTE ──
    if not REMOTE_SSH:
        issues.append(("[FAIL]", "DEPLOY_SSH_REMOTE 未设置"))
    elif "0.0.0.0" in REMOTE_SSH:
        issues.append(("[FAIL]",
            f"DEPLOY_SSH_REMOTE 中 IP 仍为占位符:\n"
            f"     {REMOTE_SSH}\n"
            f"     请把 0.0.0.0 替换为你的服务器 IP"))
    else:
        warns.append(("[PASS]", f"REMOTE_SSH = {REMOTE_SSH}"))

    # ── SSH 密钥 ──
    key_file = ssh_key if os.path.isabs(ssh_key) else os.path.join(PROJECT_DIR, ssh_key)
    if not os.path.exists(key_file):
        issues.append(("[FAIL]",
            f"SSH 密钥不存在: {key_file}\n"
            f"     生成密钥: ssh-keygen -t ed25519 -f deploy_key -N \"\"\n"
            f"     上传公钥: ssh-copy-id -i deploy_key.pub {REMOTE_SSH_USER}@{REMOTE_HOST}"))
    else:
        warns.append(("[PASS]", f"SSH 密钥: {key_file}"))

    # ── 密钥权限 (Linux/macOS 下 600) ──
    if os.path.exists(key_file) and sys.platform != "win32":
        mode = os.stat(key_file).st_mode & 0o777
        if mode != 0o600:
            warns.append(("[WARN]",
                f"SSH 密钥权限为 {oct(mode)}, 应改为 600:\n"
                f"     chmod 600 {key_file}"))

    # ── SSH 连通性 ──
    if REMOTE_HOST and REMOTE_HOST not in ("0.0.0.0", "127.0.0.1") and os.path.exists(key_file):
        connectivity_ok, err = _test_ssh_connectivity()
        if not connectivity_ok:
            issues.append(("[FAIL]",
                f"无法 SSH 连接到 {REMOTE_SSH_USER}@{REMOTE_HOST}\n"
                f"     {err}\n"
                f"     检查: IP 是否正确? 密钥是否上传? 防火墙是否允许 22 端口?"))
        else:
            warns.append(("[PASS]", f"SSH {REMOTE_SSH_USER}@{REMOTE_HOST} 连接正常"))

    # ── 输出 ──
    if issues:
        _fail("部署前置检查失败", issues)

    # 无错误, 输出确认信息
    _info("部署前置检查通过:")
    for mark, msg in warns:
        print(f"  {mark} {msg}")
    print()


def _test_ssh_connectivity():
    """测试 SSH 连接是否可达。返回 (ok, error_msg)。"""
    cmd = [
        "ssh", "-i", _get_ssh_key(),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
        f"{REMOTE_SSH_USER}@{REMOTE_HOST}", "echo pong",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and "pong" in r.stdout:
            return True, ""
        return False, r.stderr.strip() or "no response"
    except subprocess.TimeoutExpired:
        return False, "连接超时 (8s)"
    except Exception as e:
        return False, str(e)


def _get_ssh_key():
    """获取SSH密钥路径（返回前向斜杠，兼容Windows+Git Bash）。"""
    env_key = os.environ.get("DEPLOY_SSH_KEY")
    if env_key and os.path.exists(env_key):
        return env_key.replace("\\", "/")
    default = os.path.join(PROJECT_DIR, "deploy_key")
    if os.path.exists(default):
        return default.replace("\\", "/")
    return default.replace("\\", "/")  # 不存在也返回，让SSH报错


# ── SSH 工具 ────────────────────────────────────────────


def _info(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


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
        f"{REMOTE_SSH_USER}@{REMOTE_HOST}",
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

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {_get_ssh_key()} -o StrictHostKeyChecking=no"

    # 先尝试普通 push
    for force in (False, True):
        cmd = ["git", "push"]
        if force:
            cmd.append("--force")
        cmd.extend([REMOTE_SSH, "master"])

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
                tag = " (force)" if force else ""
                _info(f"Git push successful!{tag}")
                if result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        print(f"  {line}")
                return True

            # 非 fast-forward → 自动 force push
            if "non-fast-forward" in result.stderr:
                _info("Non-fast-forward detected, retrying with --force...")
                continue

            _info(f"Git push FAILED: {result.stderr}")
            print(f"  ERROR: {result.stderr}")
            return False

        except subprocess.TimeoutExpired:
            _info("Git push timed out after 120s")
            return False
        except Exception as e:
            _info(f"Git push error: {e}")
            return False

    return False


def _ensure_remote_repo():
    """确保远程服务器是git仓库并正确配置"""
    cmd = (
        f"cd {REMOTE_DIR} && "
        f"if [ ! -d .git ]; then "
        f"  git init && git config receive.denyCurrentBranch updateInstead && "
        f"  git config user.email 'deploy@trade-eyes-keeper' && "
        f"  git config user.name 'Deploy Bot' && "
        f"  echo '[OK] Git repo initialized (empty)'; "
        f"else "
        f"  echo '[OK] Git repo exists'; "
        f"fi"
    )
    success, out, _ = _ssh_cmd(cmd, "Check/init git repo on remote")
    if not success:
        return False

    # 如果仓库已有内容但无法更新工作树 (unborn HEAD / dirty working tree),
    # 重置到可接受状态
    fix_cmd = (
        f"cd {REMOTE_DIR} && "
        f"if git status --porcelain 2>/dev/null | grep -q '^'; then "
        f"  echo 'Working tree has changes, resetting...'; "
        f"  git checkout -f master 2>/dev/null || git symbolic-ref HEAD refs/heads/master; "
        f"  echo '[OK] Working tree ready'; "
        f"else "
        f"  echo '[OK] Working tree clean'; "
        f"fi"
    )
    _ssh_cmd(fix_cmd, "Ensure working tree is ready for push")
    return True


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
        "--select", "F,E,S110",
        timeout=30,
    )
    if not ok:
        if "No module named ruff" in err or "_find_ruff" in err or "Could not find" in err:
            _info("SKIP: ruff not available")
        else:
            _info(f"FAIL: ruff check failed\n{err[:500]}")
            return False
    else:
        _info("PASS: ruff check (incl. S110 try-except-pass)")

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

    # 2b. 检查无日志的 except Exception: pass 模式
    _info("Checking for silent exception swallowing...")
    ok, out, err = _run_local(
        sys.executable, "-c",
        "import subprocess, sys; "
        "r = subprocess.run("
        "['grep', '-rn', 'except Exception:\\\\s*$', 'src/'], "
        "capture_output=True, text=True); "
        "lines = r.stdout.strip().split(chr(10)) if r.stdout else []; "
        "bad = [l for l in lines if l and 'logger.' not in l]; "
        "print(f'{len(bad)} silent except:pass remaining') if bad else print('OK'); "
        "sys.exit(1) if bad else sys.exit(0)",
        timeout=15,
    )
    if not ok:
        if "OK" in (out or ""):
            _info("PASS: no silent except:pass found")
        else:
            _info(f"WARN: {out.strip()}")
            # 不阻断部署，仅警告
    else:
        _info("PASS: no silent except:pass found")

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

    # 3b. 数据源存活探针（真实 API 调用，验证数据可用性）
    # 失败不阻断部署但打出 WARN（外部依赖不可控）
    _info("Running data source health smoke tests...")
    _run_local(
        sys.executable, "-m", "pytest",
        "tests/test_data_source_health.py",
        "-m", "smoke",
        "-p", "no:capture", "-q",
        "--tb=short",
        timeout=120,
    )
    # smoke 结果通过查看上一次 run 的退出码间接判断（非严格阻断）
    ok2, smoke_out, smoke_err = _run_local(
        sys.executable, "-m", "pytest",
        "tests/test_data_source_health.py",
        "-m", "smoke",
        "-p", "no:capture", "-q",
        "--tb=line",
        timeout=120,
    )
    if not ok2:
        _info(f"WARN: data source health checks have failures (non-blocking)")
        # 打印最后 20 行供诊断
        failures = (smoke_out or "").split("\n")
        for line in failures[-15:]:
            if line.strip():
                _info(f"  {line.strip()}")
    else:
        _info("PASS: data source health smoke tests")
    _info("PASS: core tests")
    _info("All pre-deploy checks passed")
    return True


def _sync_config():
    """将本地配置文件同步到服务器。默认只同步 alerts.yaml (无敏感信息)。
    使用 --sync-config 才同步 config.yaml，--sync-env 同步 .env。
    """
    _info("Syncing config files to server...")

    ssh_key = _get_ssh_key()
    sync_env = os.environ.get("SYNC_ENV", "").strip().lower() in ("1", "true", "yes")
    sync_config = os.environ.get("SYNC_CONFIG", "").strip().lower() in ("1", "true", "yes")

    # 始终同步 alerts.yaml (技术指标，无敏感信息)
    configs = [
        ("config/alerts.yaml", "alerts.yaml"),
    ]

    # config.yaml: 含服务器特定配置 (ssl, public_ip 等)，需明确开启
    if sync_config:
        configs.append(("config/config.yaml", "config.yaml"))

    # .env: 含密码，需独立开启
    if sync_env:
        configs.append(("config/.env", ".env"))

    for local_rel, remote_name in configs:
        local_path = os.path.join(PROJECT_DIR, local_rel)
        if not os.path.exists(local_path):
            _info(f"SKIP: {local_rel} not found locally")
            continue

        remote_path = f"{REMOTE_DIR}/config/{remote_name}"
        scp_cmd = [
            "scp",
            "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            local_path,
            f"{REMOTE_SSH_USER}@{REMOTE_HOST}:{remote_path}",
        ]
        try:
            result = subprocess.run(
                scp_cmd,
                capture_output=True, text=True,
                timeout=30,
            )
            if result.returncode == 0:
                _info(f"Synced: {local_rel} -> {remote_path}")
            else:
                _info(f"FAIL to sync {local_rel}: {result.stderr[:100]}")
        except subprocess.TimeoutExpired:
            _info(f"TIMEOUT syncing {local_rel}")
        except Exception as e:
            _info(f"ERROR syncing {local_rel}: {e}")


def deploy():
    """主部署函数"""
    host = REMOTE_HOST
    dry_run = _get_dry_run()

    if dry_run:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DRY RUN MODE")
        print("=" * 70)

    _info(f"Starting CI/CD deployment to {host}")
    print("=" * 70)

    steps = {}  # step_name -> (pass/warn/fail, message)

    def _step(name, ok, msg=""):
        steps[name] = (ok, msg)
        return ok

    def _ssh_checked(cmd, desc, timeout=60):
        """SSH command that returns (success, stdout) for downstream parsing."""
        return _ssh_cmd(cmd, desc, timeout=timeout)

    def _remote_output_contains(out, marker):
        return marker in (out or "")

    try:
        # ── -1. 前置自检 (配置文件 + SSH 密钥 + 连通性) ──
        _check_prerequisites()

        # ── 0. 部署前检查 ──
        if not _pre_deploy_checks(dry_run):
            _info("ERROR: Pre-deploy checks failed, aborting deployment")
            return False

        # ── 0b. 确保远程git仓库就绪 ──
        if not _ensure_remote_repo():
            _info("WARNING: Remote repo setup may have issues")

        # ── 0c. 备份服务器 config.yaml (bot 修改不会被覆盖) ──
        _ssh_cmd(
            f"cp {REMOTE_DIR}/config/config.yaml /tmp/config.yaml.bot_backup 2>/dev/null;"
            " echo 'backed_up'",
            "Backup server config.yaml",
        )

        # ── 1. git push 本地代码到远程 ──
        pushed = _git_push()
        _step("git_push", pushed)
        if not pushed:
            _info("ERROR: Git push failed, aborting deployment")
            return _print_summary(steps, cleaning_performed=False)

        # ── 2. 检查服务器状态 ──
        _info("Checking system status...")
        _ssh_cmd("date", "Server time")
        _ssh_cmd("hostname -I", "Server IP")
        _ssh_cmd("uname -a", "System info")

        # ── 2b. 同步配置文件到服务器 ──
        _sync_config()

        # ── 2c. 恢复服务器 config.yaml (保留 bot 修改) ──
        _ssh_cmd(
            f"if [ -f /tmp/config.yaml.bot_backup ]; then"
            f" cp /tmp/config.yaml.bot_backup {REMOTE_DIR}/config/config.yaml;"
            " echo 'config_restored';"
            " else echo 'no_backup'; fi",
            "Restore server config.yaml",
        )

        # ── 3. 清理旧日志 ──
        clean = os.getenv("CLEAN_BEFORE_DEPLOY", "true").strip().lower() in (
            "1", "true", "yes",
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
        ok, _, _ = _ssh_cmd(
            f"cd {REMOTE_DIR} && pwd && git status --short",
            "Project directory and git status",
        )
        _step("project_dir", ok, "exists" if ok else "not found")

        # ── 5. 安装系统依赖 ──
        _info("Installing system dependencies (texlive for PDF)...")
        _ssh_cmd(
            f"apt install -y -qq texlive-xetex texlive-latex-recommended texlive-latex-extra 2>/dev/null || echo 'texlive install skipped'",
            "Install texlive",
            timeout=300,
        )

        # ── 6. 安装/更新 Python 依赖 ──
        _info("Installing/updating Python dependencies...")
        ok, _, _ = _ssh_cmd(
            f"cd {REMOTE_DIR} && pip install --quiet -r requirements.txt",
            "Install dependencies",
            timeout=300,
        )
        _step("deps", ok, "installed" if ok else "pip install failed")

        # ── 7. 系统测试 (真验证: 全量输出, grep 错误) ──
        _info("Running system test (SKIP_EMAIL mode, full output)...")
        sys_test_cmd = (
            f"cd {REMOTE_DIR} && "
            f"SKIP_EMAIL=true timeout 180 python3 main.py --once "
            f"> /tmp/system_test.log 2>&1; "
            f"rc=$?; "
            f"echo '---EXIT:' $rc '---'; "
            f"echo '---ERRORS---'; "
            f"grep -c -E 'ERROR|CRITICAL|Traceback|FAILED' /tmp/system_test.log 2>/dev/null || echo 0; "
            f"echo '---TAIL---'; "
            f"tail -50 /tmp/system_test.log"
        )
        ok, out, _ = _ssh_checked(sys_test_cmd, "System test", timeout=240)

        exit_code = 0
        error_count = 0
        for line in (out or "").split("\n"):
            if line.startswith("---EXIT:"):
                try:
                    exit_code = int(line.split()[1])
                except (ValueError, IndexError):
                    pass
            if line.startswith("---ERRORS---"):
                continue
            if line.strip().isdigit():
                try:
                    error_count = int(line.strip())
                except ValueError:
                    pass
                break  # 只取第一个数字（grep -c 的结果）

        if not ok or exit_code != 0 or error_count > 0:
            _step("system_test", False,
                  f"exit={exit_code} errors={error_count}")
            _info(f"FAIL: System test errors (exit={exit_code}, errors={error_count})")
            # 打印错误上下文供诊断
            if error_count > 0:
                _ssh_cmd(
                    f"grep -n -E 'ERROR|CRITICAL|Traceback' /tmp/system_test.log | head -20",
                    "Error lines from system test", timeout=30,
                )
        else:
            _step("system_test", True, f"exit=0 errors=0")

        # ── 8. 检查cron ──
        _info("Verifying cron job...")
        ok, out, _ = _ssh_cmd("crontab -l", "Current cron jobs")

        has_once = "python3 main.py --once" in (out or "")
        has_brief_morning = "--brief morning_snapshot" in (out or "")
        has_brief_afternoon = "--brief afternoon_snapshot" in (out or "")
        has_optimize = "python3 main.py --optimize" in (out or "")

        # ── 9. 更新cron（如需） ──
        if not has_once:
            _info("Configuring daily cron job...")
            cron_line = f"00 19 * * * cd {REMOTE_DIR} && python3 main.py --once >> {REMOTE_DIR}/logs/cron.log 2>&1  # Stock quantitative system"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -",
                "Add main cron",
            )
        if not has_brief_morning:
            _info("Configuring morning brief report cron at 09:50...")
            brief_line = f"50 9 * * * cd {REMOTE_DIR} && python3 main.py --brief morning_snapshot >> {REMOTE_DIR}/logs/cron_brief.log 2>&1"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{brief_line}') | crontab -",
                "Add morning brief report cron",
            )
            _info("Morning brief report cron registered (09:50 daily)")
        if not has_brief_afternoon:
            _info("Configuring afternoon brief report cron at 14:30...")
            brief_line = f"30 14 * * * cd {REMOTE_DIR} && python3 main.py --brief afternoon_snapshot >> {REMOTE_DIR}/logs/cron_brief_afternoon.log 2>&1"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{brief_line}') | crontab -",
                "Add afternoon brief report cron",
            )
            _info("Afternoon brief report cron registered (14:30 daily)")
        if not has_optimize:
            _info("Configuring strategy optimizer cron at 02:00...")
            opt_line = f"0 2 * * * cd {REMOTE_DIR} && python3 main.py --optimize >> {REMOTE_DIR}/logs/cron_optimize.log 2>&1"
            _ssh_cmd(
                f"(crontab -l 2>/dev/null; echo '{opt_line}') | crontab -",
                "Add optimizer cron",
            )
            _info("Optimizer cron registered (02:00 daily)")

        # 重新检查 cron
        ok, out, _ = _ssh_cmd("crontab -l", "Verify final cron")
        cron_ok = (
            ("--once" in (out or ""))
            and ("--brief morning_snapshot" in (out or ""))
            and ("--brief afternoon_snapshot" in (out or ""))
            and ("--optimize" in (out or ""))
        )
        _step("cron", cron_ok, "daily+brief+optimize registered" if cron_ok else "missing")

        # ── 9c. 检查优化器数据, 首次部署触发初始运行 ──
        _info("Checking optimizer data...")
        opt_check = (
            f"ls {REMOTE_DIR}/data/optimizer/*_strategies.yaml 2>/dev/null | wc -l"
        )
        _, opt_count_out, _ = _ssh_cmd(opt_check, "Count optimizer YAML files")
        opt_count = int(opt_count_out.strip() or "0")
        if opt_count == 0:
            _info("No optimizer data found — starting initial optimization (background, ~30min)...")
            _ssh_cmd(
                f"cd {REMOTE_DIR} && nohup python3 main.py --optimize "
                f"> {REMOTE_DIR}/logs/optimize_init.log 2>&1 &",
                "Start initial optimizer in background",
                timeout=10,
            )
            _info("Initial optimization running in background (check logs/optimize_init.log)")
            _step("optimizer_data", None, "initial run started (30min)")
        else:
            _info(f"Optimizer data exists ({opt_count} files)")
            _step("optimizer_data", True, f"{opt_count} strategy files")
        check_archive = f"""timeout 10 bash -c '
latest=$(ls -t {REMOTE_DIR}/data/email_archive/*.html 2>/dev/null | head -1)
if [ -n "$latest" ]; then
    echo "Latest email archive: $latest"
    if grep -q "服务器信息" "$latest"; then
         echo "[ARCHIVE_OK] Server info found in email archive"
        grep -A5 "服务器信息" "$latest" | head -10
    else
         echo "[ARCHIVE_WARN] Server info NOT found"
    fi
else
    echo "[ARCHIVE_NA] No email archives found yet"
fi
'
"""
        ok, out, _ = _ssh_cmd(check_archive, "Check email archives")
        archive_ok = "[ARCHIVE_OK]" in (out or "") or "[ARCHIVE_NA]" in (out or "")
        _step("email_archive",
              True if archive_ok else None,  # None = WARN, don't block
              "ok" if archive_ok else "no server info (may be from SKIP_EMAIL run)")

        # ── 11. 发送部署通知 (真验证: 检查 SMTP) ──
        _info("Sending deployment notification email...")
        version_cmd = f"cd {REMOTE_DIR} && git rev-parse --short HEAD 2>/dev/null || echo 'unknown'"
        _, version_out, _ = _ssh_cmd(version_cmd, "Get git version")
        version = version_out.strip() if version_out else "unknown"

        deploy_notify_script = f"""cd {REMOTE_DIR} && timeout 30 python3 -c "
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('config/.env')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
# 注入 .env 的邮件凭证 (复刻 main.py load_config)
if os.getenv('EMAIL_SENDER'):
    config.setdefault('email', {{}})['sender_email'] = os.getenv('EMAIL_SENDER')
if os.getenv('EMAIL_PASSWORD'):
    config.setdefault('email', {{}})['sender_password'] = os.getenv('EMAIL_PASSWORD')
if os.getenv('EMAIL_RECEIVER'):
    config.setdefault('email', {{}})['receiver_email'] = os.getenv('EMAIL_RECEIVER')
from src.notification.manager import NotifierManager
notifier = NotifierManager(config)
ok, msg = notifier.email.send_deployment_notification('SUCCESS', version='{version}',
                                     summary='CI/CD deployment completed successfully')
if ok:
    print('[NOTIFY_OK] Deployment notification sent')
else:
    print(f'[NOTIFY_FAIL] SMTP error: {{msg}}')
"
"""
        ok, out, _ = _ssh_cmd(deploy_notify_script, "Send deployment notification", timeout=60)
        notify_ok = "[NOTIFY_OK]" in (out or "")
        _step("deploy_notify",
              True if notify_ok else None,  # None = WARN, SMTP is non-critical
              "SMTP OK" if notify_ok else f"SMTP auth failed — check EMAIL_PASSWORD in server config/.env")

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

print(f'Health server config: enabled={{enabled}} host={{host}} port={{port}}')
print('[HS_CONFIG_OK]')
"
"""
        _ssh_cmd(health_check, "Health server config check")

        # ── 13. 停止旧健康服务器 ──
        _info("Stopping old health server...")
        stop_cmds = [
            # 用 PID 文件精确杀
            "if [ -f /tmp/hs.pid ]; then kill $(cat /tmp/hs.pid) 2>/dev/null; rm -f /tmp/hs.pid; fi",
            # 兜底：pkill 模糊匹配
            "pkill -f 'python.*main.py.*--health-server' 2>/dev/null || true",
        ]
        for cmd in stop_cmds:
            _ssh_cmd(cmd, "Stop health server processes")
        time.sleep(2)

        # ── 14. 启动健康服务器 (nohup + PID 文件，不用 screen) ──
        start_cmd = (
            f"cd {REMOTE_DIR} && "
            f"bash -c 'nohup python3 main.py --health-server > /tmp/hs.log 2>&1 & echo $! > /tmp/hs.pid'"
        )
        _ssh_cmd(start_cmd, "Start health server", timeout=10)
        time.sleep(4)

        # 验证进程存在，失败重试一次
        _ssh_cmd(
            "bash -c 'if ! kill -0 $(cat /tmp/hs.pid) 2>/dev/null; then"
            f" cd {REMOTE_DIR} &&"
            f" nohup python3 main.py --health-server > /tmp/hs.log 2>&1 &"
            f" echo $! > /tmp/hs.pid;"
            " echo RETRIED; else echo OK; fi'",
            "Verify health server alive",
        )
        time.sleep(3)

        verify_cmd = f"""cd {REMOTE_DIR} && python3 -c "
import sys
sys.path.insert(0, '.')
import yaml, urllib.request, urllib.error, time, ssl

with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

hc = config.get('health_server', {{}})
port = hc.get('port', 1933)
use_ssl = hc.get('ssl', False)

# 有 SSL 用 https, 否则用 http
scheme = 'https' if use_ssl else 'http'
ctx = ssl._create_unverified_context() if use_ssl else None

for attempt in range(6):
    time.sleep(1)
    try:
        url = scheme + '://localhost:' + str(port) + '/'
        req = urllib.request.Request(url, headers={{'User-Agent': 'CI/CD Verification'}})
        response = urllib.request.urlopen(req, timeout=5, context=ctx) if ctx else urllib.request.urlopen(req, timeout=5)
        html = response.read().decode('utf-8', errors='replace')
        if len(html) > 500:
            print('[HS_HTTP_OK] Health server responded (' + scheme + ' ' + str(len(html)) + ' bytes)')
        else:
            print('[HS_HTTP_FAIL] Response too short: ' + str(len(html)) + ' bytes')
        break
    except urllib.error.URLError as e:
        if attempt >= 5:
            print(f'[HS_HTTP_FAIL] Connection failed after 6 retries: {{e}}')
        continue
    except Exception as e:
        print(f'[HS_HTTP_FAIL] Error: {{e}}')
        break
" 2>&1"""
        ok, out, _ = _ssh_cmd(verify_cmd, "Verify health server HTTP response", timeout=30)
        hs_ok = "[HS_HTTP_OK]" in (out or "")
        _step("health_server", hs_ok,
              "HTTP 200" if hs_ok else f"fail: {(out or '')[:80]}")

        # ── 15. 最终验证 ──
        _info("Final system verification...")
        _ssh_cmd(f"cd {REMOTE_DIR} && python3 --version", "Python version")
        ok, _, _ = _ssh_cmd(
            f"cd {REMOTE_DIR} && ls -la src/notification/email_notifier.py",
            "Email notifier file",
        )
        _step("final_verify", ok, "code in place" if ok else "file missing")

        # ── 完成 ──
        return _print_summary(steps, cleaning_performed)

    except Exception as e:
        _info(f"DEPLOYMENT FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def _print_summary(steps, cleaning_performed=False):
    """根据收集的步骤结果打印真实状态汇总。"""
    print("=" * 60)
    print("  DEPLOYMENT SUMMARY")
    print("=" * 60)

    all_good = True
    for name, (ok, msg) in steps.items():
        if ok is True:
            tag = "[PASS]"
        elif ok is False:
            tag = "[FAIL]"
            all_good = False
        else:
            tag = "[WARN]"
        detail = f" — {msg}" if msg else ""
        print(f"  {tag} {name}{detail}")

    if cleaning_performed:
        print("  [INFO] Old logs/archives cleaned (30+ days)")

    print("=" * 60)
    if all_good:
        _info("Deployment completed successfully!")
    else:
        _info("Deployment completed with failures — check [FAIL] items above")
    return all_good


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
            "ss -tlnp 2>/dev/null | grep python || echo 'No python process listening (or ss not available)'",
            "Health server port check",
        )
        _ssh_cmd(
            "ps aux | grep -i 'main.py.*--health-server' | grep -v grep || echo 'No health server process found'",
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
    parser.add_argument(
        "--sync-env",
        action="store_true",
        help="Also sync config/.env to server (contains sensitive credentials, off by default)",
    )
    parser.add_argument(
        "--sync-config",
        action="store_true",
        help="Also sync config/config.yaml to server (overwrites server-specific settings like ssl/ip)",
    )

    args = parser.parse_args()

    # Update SSH key path if provided
    if args.ssh_key:
        os.environ["DEPLOY_SSH_KEY"] = args.ssh_key

    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    if args.sync_env:
        os.environ["SYNC_ENV"] = "true"

    if args.sync_config:
        os.environ["SYNC_CONFIG"] = "true"

    if args.mode == "investigate":
        success = investigate_server()
    else:
        success = deploy()

    sys.exit(0 if success else 1)
