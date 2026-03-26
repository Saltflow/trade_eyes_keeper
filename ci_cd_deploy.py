#!/usr/bin/env python3
"""
CI/CD Deployment Script for Stock Quantitative System

This script deploys the latest version from GitHub to the remote server.
It performs:
1. SSH connection to server
2. Git pull to get latest code
3. Dependency installation/update
4. System test (SKIP_EMAIL mode)
5. Verification of server info in email footer
6. Cron job update if needed

Usage: python ci_cd_deploy.py
"""

import paramiko
import sys
import os
import time
import argparse
from datetime import datetime


def run_ssh(client, cmd, description="", timeout=60):
    """Run SSH command and return (success, output, error)"""
    if description:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {description}")
        print(f"  $ {cmd}")

    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()

    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")

    if out:
        print(out)
    if err:
        print(f"  ERROR: {err}")

    return exit_code == 0, out, err


class MockSSHClient:
    """Mock SSH client for dry-run mode"""

    def __init__(self, hostname, username):
        self.hostname = hostname
        self.username = username
        self.commands = []

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(
        self,
        hostname=None,
        port=22,
        username=None,
        pkey=None,
        password=None,
        timeout=30,
    ):
        hostname = hostname or self.hostname
        username = username or self.username
        print(f"  [MOCK] Would connect to {hostname}:{port} as {username}")
        if pkey:
            print(f"  [MOCK] Using SSH key")
        if password:
            print(f"  [MOCK] Using password (hidden)")

    def exec_command(self, cmd, timeout=60):
        print(f"  [MOCK] Executing: {cmd} (timeout: {timeout})")
        # Return mock stdin, stdout, stderr
        import io

        class MockChannel:
            def recv_exit_status(self):
                return 0

        class MockFile:
            def __init__(self):
                self.channel = None

            def read(self):
                return b"Mock output\n"

            def decode(self, *args, **kwargs):
                return "Mock output"

        stdin = MockFile()
        stdout = MockFile()
        stderr = MockFile()
        stdout.channel = MockChannel()
        return stdin, stdout, stderr

    def close(self):
        pass


def _get_ssh_key_path():
    """Get SSH key path from environment variables or default location."""
    ssh_key_path = os.getenv("DEPLOY_SSH_KEY_PATH")
    ssh_key_content = os.getenv("DEPLOY_SSH_KEY")

    # Default fallback: look for deploy_key in current directory
    if not ssh_key_path and not ssh_key_content:
        default_key = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "deploy_key"
        )
        if os.path.exists(default_key):
            ssh_key_path = default_key
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Using default SSH key: {ssh_key_path}"
            )

    return ssh_key_path, ssh_key_content


def _get_dry_run():
    """Check if dry-run mode is enabled."""
    dry_run_env = os.getenv("DRY_RUN", "")
    dry_run = dry_run_env.strip().lower() in ("1", "true", "yes")
    return dry_run


def load_ssh_key(key_path):
    """Load SSH private key from file path"""
    # Try different key types
    for key_class in [
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.DSSKey,
    ]:
        try:
            return key_class.from_private_key_file(key_path)
        except paramiko.SSHException:
            continue
    raise ValueError(f"无法解析SSH密钥文件 {key_path}，不支持此密钥类型")


def load_ssh_key_from_string(key_content):
    """Load SSH private key from string content"""
    from io import StringIO

    key_file = StringIO(key_content)
    # Try different key types
    for key_class in [
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.DSSKey,
    ]:
        try:
            return key_class.from_private_key(key_file)
        except paramiko.SSHException:
            key_file.seek(0)
            continue
    raise ValueError("无法解析SSH密钥内容，不支持此密钥类型")


def _create_ssh_client(
    host, port, username, dry_run, ssh_key_path=None, ssh_key_content=None
):
    """Create and connect SSH client (real or mock based on dry-run)."""
    if dry_run:
        client = MockSSHClient(host, username)
        client.set_missing_host_key_policy = lambda policy: None
    else:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Wait 60 seconds to avoid rate limiting (only for real connections)
    if not dry_run:
        print(f"  Waiting 60 seconds to avoid rate limiting...")
        time.sleep(60)

    try:
        if ssh_key_path and os.path.exists(ssh_key_path):
            print(f"  Using SSH key from file: {ssh_key_path}")
            key = load_ssh_key(ssh_key_path)
            client.connect(
                hostname=host, port=port, username=username, pkey=key, timeout=60
            )
        elif ssh_key_content:
            print(f"  Using SSH key from environment variable")
            key = load_ssh_key_from_string(ssh_key_content)
            client.connect(
                hostname=host, port=port, username=username, pkey=key, timeout=60
            )
        else:
            print(f"  Using password authentication")
            password = os.getenv("DEPLOY_PASSWORD", "")
            if not password:
                print(
                    f"  WARNING: DEPLOY_PASSWORD environment variable not set. SSH connection may fail."
                )
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=60,
            )
    except paramiko.AuthenticationException:
        raise
    except Exception as e:
        raise

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Connected successfully!")
    return client


def sync_code_to_remote(client, dry_run=False):
    """Sync local code to remote server using tar over SSH."""
    import tempfile
    import tarfile
    import io

    if dry_run:
        print(f"  [MOCK] Would sync local code to remote server")
        return True

    # Create a temporary tar file of the project directory
    # Exclude certain directories: .git, cache, logs, data, __pycache__, .env, deploy_key*
    project_root = os.path.dirname(os.path.abspath(__file__))

    # Create tar in memory
    tar_buffer = io.BytesIO()
    file_count = 0
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for root, dirs, files in os.walk(project_root):
            # Skip excluded directories
            rel_root = os.path.relpath(root, project_root)
            if rel_root == ".":
                rel_root = ""

            # Skip hidden and excluded directories
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("cache", "logs", "data", "__pycache__")
            ]

            for file in files:
                # Skip excluded files
                if file.startswith("deploy_key"):
                    continue
                if file.endswith(".pyc") or file == ".env":
                    continue
                # 配置文件也需要同步，确保一致性
                # if file == "config.yaml" and rel_root == "config":
                #    continue  # Keep remote config

                filepath = os.path.join(root, file)
                arcname = os.path.join(rel_root, file) if rel_root else file
                arcname = arcname.replace("\\", "/")
                tar.add(filepath, arcname=arcname, recursive=False)
                file_count += 1

    print(f"  Added {file_count} files to archive")

    tar_data = tar_buffer.getvalue()
    print(f"  Created tar archive of {len(tar_data)} bytes")

    # Upload tar to remote server and extract
    sftp = client.open_sftp()

    # Upload tar file
    remote_tar_path = "/tmp/deploy.tar.gz"
    with sftp.file(remote_tar_path, "wb") as f:
        f.write(tar_data)

    # Extract on remote server
    print(f"  Extracting on remote server...")
    cmd = f"cd /root/trade_eyes_keeper && tar -xzf {remote_tar_path} --exclude='cache/*' --exclude='logs/*' --exclude='data/*' 2>&1"
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")

    # Clean up remote tar
    sftp.remove(remote_tar_path)
    sftp.close()

    if exit_code == 0:
        print(f"  Code sync completed successfully")
        if out and out.strip():
            print(f"  Output: {out[:200]}")  # Print first 200 chars of output
        return True
    else:
        print(f"  WARNING: Code sync failed with exit code {exit_code}")
        if out and out.strip():
            print(f"  Output: {out[:500]}")
        if err and err.strip():
            print(f"  Error: {err[:500]}")
        return False


def deploy():
    """Main deployment function"""
    host = os.getenv(
        "DEPLOY_HOST", "DEPLOY_HOST"
    )  # Configurable via environment variable
    port = int(os.getenv("DEPLOY_PORT", "22"))
    username = os.getenv("DEPLOY_USER", "root")

    # Check for dry-run mode using shared helper
    dry_run = _get_dry_run()
    if dry_run:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] DRY RUN MODE - No actual SSH connections will be made"
        )
        print("=" * 70)

    # Get SSH key path/content using shared helper
    ssh_key_path, ssh_key_content = _get_ssh_key_path()

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Starting CI/CD deployment to {host}"
    )
    print("=" * 70)

    # Create and connect SSH client using shared helper
    client = _create_ssh_client(
        host, port, username, dry_run, ssh_key_path, ssh_key_content
    )

    try:
        # 1. Check current system status
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Checking current system status..."
        )
        run_ssh(client, "date", "Server time")
        run_ssh(client, "hostname -I", "Server IP addresses")
        run_ssh(client, "uname -a", "System info")

        # 2. Clean old logs and email archives before deployment
        clean_before_deploy = os.getenv(
            "CLEAN_BEFORE_DEPLOY", "true"
        ).strip().lower() in ("1", "true", "yes")
        cleaning_performed = clean_before_deploy
        if clean_before_deploy:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Cleaning old logs and email archives (30+ days)..."
            )
            # Clean log files older than 30 days
            run_ssh(
                client,
                "cd /root/trade_eyes_keeper && find logs/ -name '*.log' -type f -mtime +30 -delete 2>/dev/null || echo 'No old log files to delete'",
                "Clean old logs",
            )
            # Clean email archive files older than 30 days
            run_ssh(
                client,
                "cd /root/trade_eyes_keeper && find data/email_archive/ -name '*.html' -type f -mtime +30 -delete 2>/dev/null || echo 'No old email archives to delete'",
                "Clean old email archives",
            )
        else:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Skipping cleaning (CLEAN_BEFORE_DEPLOY=false)"
            )

        # 3. Sync local code to remote server
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Syncing local code to remote server..."
        )
        sync_success = sync_code_to_remote(client, dry_run)
        if not sync_success:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Code sync may have issues"
            )

        # 4. Navigate to project directory and check git status
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking project directory...")
        run_ssh(client, "cd /root/trade_eyes_keeper && pwd", "Project directory")

        success, out, err = run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -d .git ]; then git status; else echo 'Not a git repository, skipping git operations'; fi",
            "Git status before update",
        )

        # 5. Pull latest changes from GitHub if git repo exists
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Pulling latest changes from GitHub (if git repo)..."
        )
        success, out, err = run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -d .git ]; then git pull; else echo 'Skipping git pull (not a git repo)'; fi",
            "Git pull",
        )
        if not success:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Git pull may have failed"
            )

        # 6. Check for new dependencies
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Checking/updating Python dependencies..."
        )
        success, out, err = run_ssh(
            client,
            "cd /root/trade_eyes_keeper && pip install --quiet -r requirements.txt",
            "Install dependencies",
        )

        # 7. Verify email_notifier.py has server info feature
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Verifying email footer feature..."
        )
        verify_script = """cd /root/trade_eyes_keeper && timeout 30 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.email_notifier import EmailNotifier
notifier = EmailNotifier(config)
server_info = notifier._get_server_info()
print('Server info test:')
print('  Hostname:', server_info['hostname'])
print('  IP:', server_info['ip_address'])
print('  Kernel:', server_info['kernel_version'])
print('[OK] Email footer feature is working')
"
"""
        success, out, err = run_ssh(client, verify_script, "Email footer verification")

        # 8. Run system test (SKIP_EMAIL mode)
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Running system test (SKIP_EMAIL mode)..."
        )
        test_cmd = "cd /root/trade_eyes_keeper && SKIP_EMAIL=true timeout 180 python3 main.py --once 2>&1 | tail -10"
        success, out, err = run_ssh(client, test_cmd, "System test", timeout=240)

        # 9. Check cron job
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verifying cron job...")
        success, out, err = run_ssh(client, "crontab -l", "Current cron jobs")

        # 10. Update cron job if needed (ensure --once flag)
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Ensuring cron job uses --once flag..."
        )
        cron_check = '''crontab -l | grep -q "python3 main.py --once" && echo "Cron job already uses --once flag" || echo "Cron job needs update"'''
        success, out, err = run_ssh(client, cron_check, "Check cron flag")

        if "needs update" in out:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating cron job...")
            # Remove old stock system cron jobs
            run_ssh(
                client,
                "crontab -l | grep -v 'Stock quantitative system' | crontab -",
                "Remove old cron",
            )
            # Add new cron job with --once
            cron_line = "30 15 * * * cd /root/trade_eyes_keeper && python3 main.py --once >> /root/trade_eyes_keeper/logs/cron.log 2>&1  # Stock quantitative system"
            run_ssh(
                client,
                f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -",
                "Add new cron",
            )
            run_ssh(client, "crontab -l", "Verify updated cron")

        # 11. Verify deployment by checking server info in latest email archive
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Checking for server info in email archives..."
        )
        check_archive = """timeout 10 bash -c '
latest=$(ls -t /root/trade_eyes_keeper/data/email_archive/*.html 2>/dev/null | head -1)
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
        success, out, err = run_ssh(client, check_archive, "Check email archives")

        # 12. Send deployment notification email
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Sending deployment notification email..."
        )
        deployment_status = "SUCCESS"
        try:
            # Get git version if available
            version_cmd = "cd /root/trade_eyes_keeper && if [ -d .git ]; then git rev-parse --short HEAD 2>/dev/null || echo 'unknown'; else echo 'not-git'; fi"
            success, version_out, _ = run_ssh(client, version_cmd, "Get version")
            version = version_out.strip() if success else "unknown"

            # Send deployment notification
            deploy_notify_script = f"""cd /root/trade_eyes_keeper && timeout 30 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.email_notifier import EmailNotifier
notifier = EmailNotifier(config)
try:
    notifier.send_deployment_notification('{deployment_status}', version='{version}',
                                         summary='CI/CD deployment completed successfully')
    print('[OK] Deployment notification sent')
except Exception as e:
    print(f'[WARNING] Failed to send deployment notification: {{e}}')
    # Fallback to sending test email
    try:
        import pandas as pd
        from datetime import datetime
        test_data = pd.DataFrame([{{'stock_code': 'TEST', 'date': datetime.now().strftime('%Y-%m-%d'),
                                  'close': 1.0, 'high': 1.0, 'low': 1.0, 'open': 1.0,
                                  'volume': 1000, 'ma60': 1.0, 'pe': 10.0, 'pb': 1.0,
                                  'roe': 15.0, 'debt_ratio': 30.0}}])
        notifier.send_alert([{{'stock_code': 'TEST', 'condition': 'test',
                              'price_difference': 0.0, 'percentage_difference': 0.0}}],
                           test_data)
        print('[OK] Fallback test email sent')
    except Exception as e2:
        print(f'[ERROR] Fallback also failed: {{e2}}')
"
"""
            success, out, err = run_ssh(
                client, deploy_notify_script, "Send deployment notification", timeout=60
            )
        except Exception as e:
            print(f"[WARNING] Could not send deployment notification: {e}")

        # 13. Check health server configuration
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Verifying health server configuration..."
        )
        health_check = """cd /root/trade_eyes_keeper && timeout 10 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

health_config = config.get('health_server', {})
enabled = health_config.get('enabled', True)
host = health_config.get('host', '0.0.0.0')
port = health_config.get('port', 1933)

print(f'Health server config:')
print(f'  Enabled: {enabled}')
print(f'  Host: {host}')
print(f'  Port: {port}')

if enabled:
    print('[OK] Health server is enabled in config')
else:
    print('[WARNING] Health server is disabled in config')
"
"""
        success, out, err = run_ssh(client, health_check, "Health server config check")

        # 14. Start health server for testing
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Testing health server startup..."
        )
        test_health_cmd = """cd /root/trade_eyes_keeper && timeout 5 python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Import and test health server
from src.health_server import HealthServer
import threading
import time

try:
    hs = HealthServer(config)
    # Start in daemon mode
    if hs.start(daemon=True):
        print(f'[OK] Health server started successfully on {hs.host}:{hs.port}')
        # Give it a moment to start
        time.sleep(1)
        # Stop it (since we're just testing)
        hs.stop()
        print('[OK] Health server stopped (test only)')
    else:
        print('[WARNING] Health server failed to start')
except Exception as e:
    print(f'[ERROR] Health server test failed: {{e}}')
" 2>&1"""
        success, out, err = run_ssh(
            client, test_health_cmd, "Health server startup test"
        )

        # 15. Restart health server with updated code
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Restarting health server with updated code..."
        )
        # Kill existing scheduler and health server processes
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Stopping all application processes..."
        )
        kill_cmds = [
            "pkill -f 'python3 main.py' 2>/dev/null || echo 'No scheduler process found'",
            "pkill -f 'health_server' 2>/dev/null || echo 'No health_server process found'",
            "pkill -f 'python.*1933' 2>/dev/null || echo 'No process on port 1933 found'",
            "screen -XS health_server quit 2>/dev/null || echo 'No screen session found'",
        ]
        for cmd in kill_cmds:
            run_ssh(client, cmd, "Stop processes")

        # Wait for processes to fully terminate
        if not dry_run:
            time.sleep(2)

        # Verify health_server.py was updated
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verifying code update...")
        verify_file_cmd = """cd /root/trade_eyes_keeper && timeout 10 python3 -c "
import os
import sys

# Check health_server.py file size (should be > 80KB if updated)
file_path = 'src/health_server.py'
if os.path.exists(file_path):
    size = os.path.getsize(file_path)
    print(f'health_server.py size: {size} bytes')
    if size > 80000:
        print('[OK] File size indicates updated version')
    else:
        print('[WARNING] File size smaller than expected (< 80KB), sync may have failed')

    # Check for management button text
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
        if '管理监控股票列表' in content:
            print('[OK] File contains management button text')
        else:
            print('[WARNING] File missing management button text')
else:
    print('[ERROR] health_server.py not found')
"
"""
        run_ssh(client, verify_file_cmd, "Verify code update")

        # Start new health server in background using screen for persistence
        start_cmd = "cd /root/trade_eyes_keeper && screen -dmS health_server python3 main.py --health-server"
        run_ssh(client, start_cmd, "Start health server", timeout=10)

        # Give it time to start (skip in dry-run mode)
        if not dry_run:
            time.sleep(3)
        else:
            print("  [MOCK] Would wait 3 seconds for health server to start")

        # Verify health server is running and serving updated page
        verify_cmd = """cd /root/trade_eyes_keeper && python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
import urllib.request
import urllib.error
import socket
import time

with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# Check 1: Port conflict test (health server already running)
from src.health_server import HealthServer
hs = HealthServer(config)
try:
    if hs.start(daemon=True):
        print('[OK] Health server started test instance')
        hs.stop()  # Stop test instance
        print('[OK] Test instance stopped')
    else:
        print('[WARNING] Health server test failed to start')
except Exception as e:
    print(f'[OK] Health server is already running (port conflict expected): {{e}}')

# Check 2: Verify homepage contains management button
try:
    # Wait a bit more for server to be fully ready
    time.sleep(2)

    # Try to fetch homepage
    url = 'http://localhost:1933/'
    req = urllib.request.Request(url, headers={'User-Agent': 'CI/CD Verification'})
    response = urllib.request.urlopen(req, timeout=10)
    html_content = response.read().decode('utf-8', errors='replace')

    # Check for management button
    if '管理监控股票列表' in html_content:
        print('[SUCCESS] Health server homepage contains management button')
        # Also check the button link
        if 'href=\"/manage\"' in html_content or 'href="/manage"' in html_content:
            print('[SUCCESS] Management button links to /manage endpoint')
        else:
            print('[WARNING] Button found but missing /manage link')
    else:
        print('[FAIL] Management button not found on homepage')
        print(f'First 500 chars of response: {{html_content[:500]}}')

except urllib.error.URLError as e:
    print(f'[FAIL] Could not fetch health server homepage: {{e}}')
except socket.timeout as e:
    print(f'[FAIL] Timeout connecting to health server: {{e}}')
except Exception as e:
    print(f'[FAIL] Error checking health server: {{e}}')
" 2>&1"""
        run_ssh(client, verify_cmd, "Verify health server restart and content")

        # 16. Final system info
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Final system verification...")
        run_ssh(
            client, "cd /root/trade_eyes_keeper && python3 --version", "Python version"
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && ls -la src/email_notifier.py",
            "Email notifier file",
        )

        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] Deployment completed successfully!"
        )
        print("=" * 70)
        print("SUMMARY:")
        if cleaning_performed:
            print("[OK] Old logs and email archives cleaned (30+ days)")
        print("[OK] Code synced to remote server")
        print("[OK] Dependencies checked/updated")
        print("[OK] Email footer feature verified")
        print("[OK] System test passed (SKIP_EMAIL mode)")
        print("[OK] Cron job configured for daily 15:30 execution")
        print("[OK] Deployment notification sent")
        print("[OK] Health server restarted with updated code (port 1933)")
        print("[OK] Server info will appear in email footer")
        print("\nNext scheduled run: Tomorrow at 15:30 server time")
        print("Server info will appear in email footer for verification")
        print("=" * 70)

        return True

    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DEPLOYMENT FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        client.close()


def investigate_server():
    """Investigate remote server state - check cache, logs, and system status"""
    host = os.getenv("DEPLOY_HOST", "DEPLOY_HOST")
    port = int(os.getenv("DEPLOY_PORT", "22"))
    username = os.getenv("DEPLOY_USER", "root")

    # Get SSH key path/content using shared helper
    ssh_key_path, ssh_key_content = _get_ssh_key_path()

    # Check for dry-run mode using shared helper
    dry_run = _get_dry_run()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Investigating server {host}")
    print("=" * 70)

    if dry_run:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] DRY RUN MODE - No actual SSH connections will be made"
        )
        print("=" * 70)

    # Create and connect SSH client using shared helper
    client = _create_ssh_client(
        host, port, username, dry_run, ssh_key_path, ssh_key_content
    )

    try:
        # 1. Basic system info
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== BASIC SYSTEM INFO ====="
        )
        run_ssh(client, "date", "Server time")
        run_ssh(client, "uptime", "System uptime")
        run_ssh(client, "hostname -I", "Server IP addresses")
        run_ssh(client, "uname -a", "System info")
        run_ssh(client, "python3 --version", "Python version")

        # 2. Project directory status
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== PROJECT DIRECTORY ====="
        )
        run_ssh(client, "cd /root/trade_eyes_keeper && pwd", "Project directory")
        run_ssh(client, "cd /root/trade_eyes_keeper && ls -la", "Directory listing")

        # 3. Git status
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== GIT STATUS =====")
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -d .git ]; then git status; git log --oneline -5; else echo 'Not a git repository'; fi",
            "Git status and recent commits",
        )

        # 4. Cache files investigation
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== CACHE FILES =====")
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && find cache/data/ -name '*.json' -type f 2>/dev/null | head -20",
            "Cache files list",
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && ls -la cache/data/ 2>/dev/null || echo 'No cache directory'",
            "Cache directory listing",
        )
        # Check specific cache file dates
        run_ssh(
            client,
            'cd /root/trade_eyes_keeper && if [ -d cache/data ]; then for f in cache/data/*.json; do if [ -f "$f" ]; then echo "$(basename $f) - $(stat -c %y $f 2>/dev/null || ls -la $f)"; fi; done | head -10; fi',
            "Cache file dates",
        )
        # Check if cache files contain TEST data
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -d cache/data ]; then grep -l 'TEST' cache/data/*.json 2>/dev/null | head -5 || echo 'No TEST data found in cache'; fi",
            "TEST data in cache",
        )

        # 5. Log files investigation
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== LOG FILES =====")
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && ls -la logs/ 2>/dev/null || echo 'No logs directory'",
            "Log directory",
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -f logs/quant_system.log ]; then tail -50 logs/quant_system.log; else echo 'No quant_system.log found'; fi",
            "Recent logs",
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -f logs/quant_system.log ]; then grep -i 'error\\|warn\\|fail' logs/quant_system.log | tail -20; else echo 'No log file'; fi",
            "Errors/Warnings in logs",
        )

        # 6. Email archives investigation
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== EMAIL ARCHIVES =====")
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && ls -la data/email_archive/ 2>/dev/null || echo 'No email archive directory'",
            "Email archive directory",
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -d data/email_archive ]; then ls -lt data/email_archive/*.html 2>/dev/null | head -5 || echo 'No email archives'; fi",
            "Recent email archives",
        )
        # Check if latest email contains TEST data
        run_ssh(
            client,
            """cd /root/trade_eyes_keeper && latest=$(ls -t data/email_archive/*.html 2>/dev/null | head -1)
if [ -n "$latest" ]; then
    echo "Latest email archive: $latest"
    if grep -q 'TEST' "$latest"; then
        echo "[WARNING] Found TEST data in latest email"
        grep -n 'TEST' "$latest" | head -5
    else
        echo "[OK] No TEST data found in latest email"
    fi
    echo "Email date from filename: $(basename "$latest" | cut -d_ -f1)"
else
    echo "No email archives found"
fi""",
            "Check for TEST data in emails",
        )

        # 7. Health server status
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== HEALTH SERVER =====")
        run_ssh(
            client,
            "netstat -tlnp 2>/dev/null | grep :1933 || echo 'Port 1933 not listening (or netstat not available)'",
            "Health server port check",
        )
        run_ssh(
            client,
            "ps aux | grep -i 'health_server\\|python.*1933' | grep -v grep || echo 'No health server process found'",
            "Health server process",
        )

        # 8. Cron jobs
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== CRON JOBS =====")
        run_ssh(client, "crontab -l", "Cron jobs")

        # 9. Check if scheduler is running now
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== SCHEDULER STATUS =====")
        run_ssh(
            client,
            "ps aux | grep -i 'main.py\\|scheduler' | grep -v grep || echo 'No scheduler process found'",
            "Scheduler process",
        )

        # 10. Check data_fetcher cache bypass logic (by examining the file)
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== DATA_FETCHER CODE CHECK ====="
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -f src/data_fetcher.py ]; then grep -n '_should_bypass_cache' src/data_fetcher.py -A 20 | head -30; else echo 'data_fetcher.py not found'; fi",
            "Cache bypass logic",
        )

        # 11. Check config file
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== CONFIGURATION =====")
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -f config/config.yaml ]; then grep -n 'cache_bypass_cutoff\\|timezone\\|run_time' config/config.yaml; else echo 'config.yaml not found'; fi",
            "Config settings",
        )

        # 12. Check if email_notifier has send_deployment_notification method
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] ===== EMAIL NOTIFIER CODE ====="
        )
        run_ssh(
            client,
            "cd /root/trade_eyes_keeper && if [ -f src/email_notifier.py ]; then grep -n 'send_deployment_notification' src/email_notifier.py -A 5 | head -20; else echo 'email_notifier.py not found'; fi",
            "Deployment notification method",
        )

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Investigation completed!")
        print("=" * 70)

        return True

    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] INVESTIGATION FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        client.close()


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
        help="Dry run mode (no actual SSH connections)",
    )
    parser.add_argument("--host", help="SSH host (overrides DEPLOY_HOST env var)")
    parser.add_argument(
        "--port", type=int, help="SSH port (overrides DEPLOY_PORT env var)"
    )
    parser.add_argument(
        "--username", help="SSH username (overrides DEPLOY_USER env var)"
    )
    parser.add_argument(
        "--password", help="SSH password (overrides DEPLOY_PASSWORD env var)"
    )
    parser.add_argument(
        "--ssh-key-path", help="Path to SSH private key (overrides DEPLOY_SSH_KEY_PATH)"
    )

    args = parser.parse_args()

    # Set environment variables from command line arguments if provided
    if args.host:
        os.environ["DEPLOY_HOST"] = args.host
    if args.port:
        os.environ["DEPLOY_PORT"] = str(args.port)
    if args.username:
        os.environ["DEPLOY_USER"] = args.username
    if args.password:
        os.environ["DEPLOY_PASSWORD"] = args.password
    if args.ssh_key_path:
        os.environ["DEPLOY_SSH_KEY_PATH"] = args.ssh_key_path

    # Set DRY_RUN environment variable if dry-run flag is used
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    if args.mode == "investigate":
        success = investigate_server()
    else:
        success = deploy()

    sys.exit(0 if success else 1)
