#!/usr/bin/env python3
"""
Unit tests for CI/CD deployment module.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, Mock
import tempfile
import shutil
from pathlib import Path

# Set environment variables for testing
os.environ['SKIP_EMAIL'] = 'true'
os.environ['DRY_RUN'] = 'true'

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

class TestCiCdDeployment(unittest.TestCase):
    """Test cases for CI/CD deployment functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        
        # Mock config
        self.config = {
            'stocks': ['601728', '600938'],
            'email': {
                'sender_email': 'test@example.com',
                'sender_password': 'test_password',
                'receiver_email': 'receiver@example.com'
            },
            'scheduler': {
                'run_time': '15:30',
                'timezone': 'Asia/Shanghai'
            }
        }
        
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('paramiko.SSHClient')
    def test_ssh_connection_mock(self, mock_ssh_client):
        """Test SSH connection mocking."""
        from ci_cd_deploy import run_ssh
        
        # Create mock SSH client
        mock_client = MagicMock()
        mock_stdin = MagicMock()
        mock_stdout = MagicMock()
        mock_stderr = MagicMock()
        mock_channel = MagicMock()
        
        # Setup mock return values
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stdout.read.return_value = b"Mock output"
        mock_stderr.read.return_value = b""
        
        # Test run_ssh function
        success, output, error = run_ssh(mock_client, "ls -la", "Test command")
        
        self.assertTrue(success)
        self.assertEqual(output, "Mock output")
        self.assertEqual(error, "")
        mock_client.exec_command.assert_called_once_with("ls -la")
    
    @patch('paramiko.SSHClient')
    def test_deploy_function_dry_run(self, mock_ssh_client):
        """Test deploy function in dry-run mode."""
        # Set DRY_RUN environment variable
        os.environ['DRY_RUN'] = 'true'
        
        # Import and test
        with patch('ci_cd_deploy.os.getenv') as mock_getenv:
            mock_getenv.side_effect = lambda key, default=None: {
                'DEPLOY_HOST': 'DEPLOY_HOST',
                'DRY_RUN': 'true',
                'DEPLOY_PASSWORD': ''
            }.get(key, default)
            
            # Mock paramiko client to ensure it's not actually used
            mock_client_instance = MagicMock()
            mock_client_instance.close = MagicMock()  # Add close method
            mock_ssh_client.return_value = mock_client_instance
            
            # Import and run deploy
            from ci_cd_deploy import deploy
            
            # Should not raise exceptions in dry-run mode
            try:
                deploy()
                # In dry-run mode, paramiko should not be called
                mock_ssh_client.assert_not_called()
            except Exception as e:
                self.fail(f"deploy() raised unexpected exception in dry-run mode: {e}")
    
    def test_run_ssh_command_success(self):
        """Test run_ssh function with successful command."""
        from ci_cd_deploy import run_ssh
        
        # Create mock SSH client
        mock_client = MagicMock()
        mock_stdin = MagicMock()
        mock_stdout = MagicMock()
        mock_stderr = MagicMock()
        mock_channel = MagicMock()
        
        # Setup mock return values for success
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stdout.read.return_value = b"Command successful\nOutput line 1\nOutput line 2"
        mock_stderr.read.return_value = b""
        
        success, output, error = run_ssh(mock_client, "test command", "Test description")
        
        self.assertTrue(success)
        self.assertIn("Command successful", output)
        self.assertEqual(error, "")
        mock_client.exec_command.assert_called_once_with("test command")
    
    def test_run_ssh_command_failure(self):
        """Test run_ssh function with failed command."""
        from ci_cd_deploy import run_ssh
        
        # Create mock SSH client
        mock_client = MagicMock()
        mock_stdin = MagicMock()
        mock_stdout = MagicMock()
        mock_stderr = MagicMock()
        
        # Setup mock return values for failure
        mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        mock_stdout.channel.recv_exit_status.return_value = 1
        mock_stdout.read.return_value = b""
        mock_stderr.read.return_value = b"Command failed: Permission denied"
        
        success, output, error = run_ssh(mock_client, "rm /root", "Dangerous command")
        
        self.assertFalse(success)
        self.assertEqual(output, "")
        self.assertIn("Permission denied", error)
    
    def test_deployment_notification_email(self):
        """Test email notification for deployments - skipped as deployment notifications removed."""
        self.skipTest("Deployment email notifications removed in simplified EmailNotifier")
    
    def test_environment_variable_loading(self):
        """Test that environment variables are properly loaded."""
        from ci_cd_deploy import deploy
        
        with patch('ci_cd_deploy.os.getenv') as mock_getenv:
            # Test with all environment variables set
            mock_getenv.side_effect = lambda key, default=None: {
                'DEPLOY_HOST': 'test.example.com',
                'DEPLOY_PASSWORD': 'test_password_123',
                'DRY_RUN': 'false'
            }.get(key, default)
            
            # Just verify getenv is called with expected keys
            # We can't actually run deploy without mocking SSH
            
            # Check that getenv is called for required variables
            calls = []
            for call in mock_getenv.call_args_list:
                calls.append(call[0][0] if call[0] else call[0])
            
            # Should have been called at least for DEPLOY_HOST
            self.assertIn('DEPLOY_HOST', [c[0] if isinstance(c, tuple) else c for c in calls])


if __name__ == '__main__':
    unittest.main()