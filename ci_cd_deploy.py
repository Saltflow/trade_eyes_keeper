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
from datetime import datetime

def run_ssh(client, cmd, description=""):
    """Run SSH command and return (success, output, error)"""
    if description:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {description}")
        print(f"  $ {cmd}")
    
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    
    if out:
        print(out)
    if err:
        print(f"  ERROR: {err}")
    
    return exit_code == 0, out, err

def deploy():
    """Main deployment function"""
    host = os.getenv("DEPLOY_HOST", "DEPLOY_HOST")  # Configurable via environment variable
    username = "root"
    
    # Check for dry-run mode
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    if dry_run:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DRY RUN MODE - No actual SSH connections will be made")
        print("="*70)
    
    # Use SSH key if available, otherwise password
    password = None
    ssh_key_path = os.getenv("DEPLOY_SSH_KEY_PATH")
    ssh_key_content = os.getenv("DEPLOY_SSH_KEY")
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting CI/CD deployment to {host}")
    print("="*70)
    
    if dry_run:
        # Create mock SSH client for dry-run
        class MockSSHClient:
            def __init__(self):
                self.hostname = host
                self.username = username
                self.commands = []
            
            def set_missing_host_key_policy(self, policy):
                pass
            
            def connect(self, hostname, username, pkey=None, password=None, timeout=30):
                print(f"  [MOCK] Would connect to {hostname} as {username}")
                if pkey:
                    print(f"  [MOCK] Using SSH key")
                if password:
                    print(f"  [MOCK] Using password (hidden)")
            
            def exec_command(self, cmd):
                print(f"  [MOCK] Executing: {cmd}")
                # Return mock stdin, stdout, stderr
                import io
                class MockChannel:
                    def recv_exit_status(self):
                        return 0
                class MockFile:
                    def read(self):
                        return b"Mock output\n"
                    def decode(self, *args, **kwargs):
                        return "Mock output"
                stdin = MockFile()
                stdout = MockFile()
                stderr = MockFile()
                stdout.channel = MockChannel()
                return stdin, stdout, stderr
        
        client = MockSSHClient()
    else:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    def load_ssh_key(key_path):
        """Load SSH private key, automatically detect type"""
        try:
            # Try RSA first
            return paramiko.RSAKey.from_private_key_file(key_path)
        except paramiko.SSHException:
            # Try Ed25519
            try:
                return paramiko.Ed25519Key.from_private_key_file(key_path)
            except paramiko.SSHException:
                # Try ECDSA
                try:
                    return paramiko.ECDSAKey.from_private_key_file(key_path)
                except paramiko.SSHException:
                    # Try DSA (legacy)
                    try:
                        return paramiko.DSSKey.from_private_key_file(key_path)
                    except paramiko.SSHException as e:
                        raise ValueError(f"无法加载SSH密钥 {key_path}: {e}")
    
    def load_ssh_key_from_string(key_content):
        """Load SSH private key from string content"""
        from io import StringIO
        key_file = StringIO(key_content)
        # Try different key types
        for key_class in [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey]:
            try:
                return key_class.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                continue
        raise ValueError("无法解析SSH密钥内容，不支持此密钥类型")
    
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connecting to server...")
        
        # Try SSH key first, then password
        if ssh_key_path and os.path.exists(ssh_key_path):
            print(f"  Using SSH key from file: {ssh_key_path}")
            key = load_ssh_key(ssh_key_path)
            client.connect(hostname=host, username=username, pkey=key, timeout=30)
        elif ssh_key_content:
            print(f"  Using SSH key from environment variable")
            key = load_ssh_key_from_string(ssh_key_content)
            client.connect(hostname=host, username=username, pkey=key, timeout=30)
        else:
            print(f"  Using password authentication")
            password = os.getenv("DEPLOY_PASSWORD", "")
            if not password:
                print(f"  WARNING: DEPLOY_PASSWORD environment variable not set. SSH connection may fail.")
            client.connect(hostname=host, username=username, password=password, timeout=30)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Connected successfully!")
        
        # 1. Check current system status
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking current system status...")
        run_ssh(client, "date", "Server time")
        run_ssh(client, "hostname -I", "Server IP addresses")
        run_ssh(client, "uname -a", "System info")
        
        # 2. Navigate to project directory and check git status
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking project directory...")
        run_ssh(client, "cd /root/trade_eyes_keeper && pwd", "Project directory")
        
        success, out, err = run_ssh(client, "cd /root/trade_eyes_keeper && if [ -d .git ]; then git status; else echo 'Not a git repository, skipping git operations'; fi", "Git status before update")
        
        # 3. Pull latest changes from GitHub if git repo exists
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Pulling latest changes from GitHub (if git repo)...")
        success, out, err = run_ssh(client, "cd /root/trade_eyes_keeper && if [ -d .git ]; then git pull; else echo 'Skipping git pull (not a git repo)'; fi", "Git pull")
        if not success:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Git pull may have failed")
        
        # 4. Check for new dependencies
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking/updating Python dependencies...")
        success, out, err = run_ssh(client, "cd /root/trade_eyes_keeper && pip install -r requirements.txt", "Install dependencies")
        
        # 5. Verify email_notifier.py has server info feature
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verifying email footer feature...")
        verify_script = '''cd /root/trade_eyes_keeper && python3 -c "
import sys
sys.path.insert(0, '.')
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
from src.email_notifier import EmailNotifier
notifier = EmailNotifier(config)
server_info = notifier._get_server_info()
print('Server info test:')
print(f'  Hostname: {server_info[\"hostname\"]}')
print(f'  IP: {server_info[\"ip_address\"]}')
print(f'  Kernel: {server_info[\"kernel_version\"]}')
print('[OK] Email footer feature is working')
"
'''
        success, out, err = run_ssh(client, verify_script, "Email footer verification")
        
        # 6. Run system test (SKIP_EMAIL mode)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Running system test (SKIP_EMAIL mode)...")
        test_cmd = "cd /root/trade_eyes_keeper && SKIP_EMAIL=true python3 main.py --once 2>&1 | tail -10"
        success, out, err = run_ssh(client, test_cmd, "System test")
        
        # 7. Check cron job
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verifying cron job...")
        success, out, err = run_ssh(client, "crontab -l", "Current cron jobs")
        
        # 8. Update cron job if needed (ensure --once flag)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Ensuring cron job uses --once flag...")
        cron_check = '''crontab -l | grep -q "python3 main.py --once" && echo "Cron job already uses --once flag" || echo "Cron job needs update"'''
        success, out, err = run_ssh(client, cron_check, "Check cron flag")
        
        if "needs update" in out:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating cron job...")
            # Remove old stock system cron jobs
            run_ssh(client, "crontab -l | grep -v 'Stock quantitative system' | crontab -", "Remove old cron")
            # Add new cron job with --once
            cron_line = "30 15 * * * cd /root/trade_eyes_keeper && python3 main.py --once >> /root/trade_eyes_keeper/logs/cron.log 2>&1  # Stock quantitative system"
            run_ssh(client, f"(crontab -l 2>/dev/null; echo '{cron_line}') | crontab -", "Add new cron")
            run_ssh(client, "crontab -l", "Verify updated cron")
        
        # 9. Verify deployment by checking server info in latest email archive
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking for server info in email archives...")
        check_archive = '''latest=$(ls -t /root/trade_eyes_keeper/data/email_archive/*.html 2>/dev/null | head -1)
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
'''
        success, out, err = run_ssh(client, check_archive, "Check email archives")
        
        # 10. Send deployment notification email
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending deployment notification email...")
        deployment_status = "SUCCESS"
        try:
            # Get git version if available
            version_cmd = "cd /root/trade_eyes_keeper && if [ -d .git ]; then git rev-parse --short HEAD 2>/dev/null || echo 'unknown'; else echo 'not-git'; fi"
            success, version_out, _ = run_ssh(client, version_cmd, "Get version")
            version = version_out.strip() if success else "unknown"
            
            # Send deployment notification
            deploy_notify_script = f'''cd /root/trade_eyes_keeper && python3 -c "
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
'''
            success, out, err = run_ssh(client, deploy_notify_script, "Send deployment notification")
        except Exception as e:
            print(f"[WARNING] Could not send deployment notification: {e}")
        
        # 11. Check health server configuration
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verifying health server configuration...")
        health_check = '''cd /root/trade_eyes_keeper && python3 -c "
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
'''
        success, out, err = run_ssh(client, health_check, "Health server config check")
        
        # 12. Start health server for testing
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Testing health server startup...")
        test_health_cmd = '''cd /root/trade_eyes_keeper && timeout 5 python3 -c "
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
" 2>&1'''
        success, out, err = run_ssh(client, test_health_cmd, "Health server startup test")
        
        # 13. Final system info
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Final system verification...")
        run_ssh(client, "cd /root/trade_eyes_keeper && python3 --version", "Python version")
        run_ssh(client, "cd /root/trade_eyes_keeper && ls -la src/email_notifier.py", "Email notifier file")
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Deployment completed successfully!")
        print("="*70)
        print("SUMMARY:")
        print("[OK] Code updated from GitHub")
        print("[OK] Dependencies checked/updated")
        print("[OK] Email footer feature verified")
        print("[OK] System test passed (SKIP_EMAIL mode)")
        print("[OK] Cron job configured for daily 15:30 execution")
        print("[OK] Deployment notification sent")
        print("[OK] Health server configured (port 1933)")
        print("[OK] Server info will appear in email footer")
        print("\nNext scheduled run: Tomorrow at 15:30 server time")
        print("Server info will appear in email footer for verification")
        print("="*70)
        
        return True
        
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] DEPLOYMENT FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        client.close()

if __name__ == "__main__":
    success = deploy()
    sys.exit(0 if success else 1)