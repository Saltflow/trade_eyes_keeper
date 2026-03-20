#!/usr/bin/env python3
"""
Unit tests for email deployment notifications.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, Mock
import tempfile
import shutil
from pathlib import Path

# Set environment variable to skip email sending
os.environ['SKIP_EMAIL'] = 'true'

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

class TestEmailDeployment(unittest.TestCase):
    """Test cases for email deployment notifications."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        
        # Test configuration
        self.config = {
            'email': {
                'sender_email': 'test@example.com',
                'sender_password': 'test_password',
                'receiver_email': 'receiver@example.com',
                'smtp_server': 'smtp.example.com',
                'smtp_port': 465,
                'enable_ssl': True
            },
            'stocks': ['601728', '600938']
        }
        
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_email_notifier_initialization(self):
        """Test EmailNotifier initialization."""
        from src.email_notifier import EmailNotifier
        
        notifier = EmailNotifier(self.config)
        
        # Check configuration
        self.assertEqual(notifier.sender_email, 'test@example.com')
        self.assertEqual(notifier.receiver_email, 'receiver@example.com')
        self.assertEqual(notifier.smtp_server, 'smtp.example.com')
        self.assertEqual(notifier.smtp_port, 465)
        self.assertTrue(notifier.enable_ssl)
        
        # Check archive directory was created
        self.assertTrue(notifier.email_archive_dir.exists())
    
    @patch('src.email_notifier.smtplib.SMTP_SSL')
    def test_send_email_success(self, mock_smtp_ssl):
        """Test successful email sending."""
        from src.email_notifier import EmailNotifier
        
        notifier = EmailNotifier(self.config)
        
        # Mock SMTP server
        mock_server = MagicMock()
        mock_smtp_ssl.return_value = mock_server
        
        # Temporarily disable SKIP_EMAIL for this test
        with patch.dict('os.environ', {'SKIP_EMAIL': ''}):
            # Send test email using internal _send_email method
            result = notifier._send_email(
                subject="Test Subject",
                body="<p>Test HTML Body</p>"
            )
        
        # Verify SMTP was called correctly
        mock_smtp_ssl.assert_called_once_with(
            notifier.smtp_server,
            notifier.smtp_port,
            context=Mock()
        )
        mock_server.login.assert_called_once_with(
            notifier.sender_email,
            notifier.sender_password
        )
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()
        
        # _send_email returns None on success
        self.assertIsNone(result)
    
    @patch('src.email_notifier.smtplib.SMTP_SSL')
    def test_send_email_failure(self, mock_smtp_ssl):
        """Test email sending failure."""
        from src.email_notifier import EmailNotifier
        
        notifier = EmailNotifier(self.config)
        
        # Mock SMTP server to raise exception
        mock_server = MagicMock()
        mock_server.login.side_effect = Exception("Authentication failed")
        mock_smtp_ssl.return_value = mock_server
        
        # Temporarily disable SKIP_EMAIL for this test
        with patch.dict('os.environ', {'SKIP_EMAIL': ''}):
            # Send test email using internal _send_email method
            # Should raise exception
            with self.assertRaises(Exception):
                notifier._send_email(
                    subject="Test Subject",
                    body="Test Body"
                )
    
    # Removed test_get_server_info - method _get_server_info no longer exists in EmailNotifier
    
    # Removed test_send_deployment_notification_success - method send_deployment_notification no longer exists in EmailNotifier
    
    # Removed test_send_deployment_notification_failure - method send_deployment_notification no longer exists in EmailNotifier
    
    def test_email_archive_creation(self):
        """Test email archiving functionality."""
        from src.email_notifier import EmailNotifier
        import datetime
        
        notifier = EmailNotifier(self.config)
        
        # Create test email content
        test_html = "<h1>Test Email</h1><p>Test content</p>"
        
        # Archive email using _save_email_copy method
        # This method is called by _send_email, but we can test it directly
        notifier._save_email_copy("test-subject", test_html)
        
        # Check that archive directory exists and has files
        self.assertTrue(notifier.email_archive_dir.exists())
        
        # List files in archive directory
        archive_files = list(notifier.email_archive_dir.glob("*.html"))
        self.assertGreater(len(archive_files), 0)
        
        # Check latest file contains our content
        latest_file = max(archive_files, key=lambda p: p.stat().st_mtime)
        with open(latest_file, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn("Test Email", content)


if __name__ == '__main__':
    unittest.main()