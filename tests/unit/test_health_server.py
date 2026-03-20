#!/usr/bin/env python3
"""
Unit tests for health server module.
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

class TestHealthServer(unittest.TestCase):
    """Test cases for HealthServer class."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create a minimal config
        self.config = {
            'stocks': ['601728', '600938', '601390'],
            'scheduler': {
                'run_time': '15:30',
                'timezone': 'Asia/Shanghai'
            },
            'health_server': {
                'enabled': True,
                'host': '0.0.0.0',
                'port': 1933
            },
            'storage': {
                'cache_dir': './cache',
                'data_dir': './data',
                'log_dir': './logs'
            }
        }
        
        # Create temporary directories
        self.temp_dir = tempfile.mkdtemp()
        self.config['storage']['cache_dir'] = os.path.join(self.temp_dir, 'cache')
        self.config['storage']['data_dir'] = os.path.join(self.temp_dir, 'data')
        self.config['storage']['log_dir'] = os.path.join(self.temp_dir, 'logs')
        
        for dir_path in ['cache', 'data', 'logs']:
            os.makedirs(os.path.join(self.temp_dir, dir_path), exist_ok=True)
    
    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_health_server_initialization(self):
        """Test HealthServer initialization with config."""
        from src.health_server import HealthServer
        
        # Test with default config
        hs = HealthServer(self.config)
        self.assertEqual(hs.host, '0.0.0.0')
        self.assertEqual(hs.port, 1933)
        self.assertIsNotNone(hs.config)
        
        # Test with custom host/port (config overrides parameters)
        hs2 = HealthServer(self.config, host='127.0.0.1', port=8080)
        self.assertEqual(hs2.host, '0.0.0.0')  # Config overrides parameter
        self.assertEqual(hs2.port, 1933)       # Config overrides parameter
        
        # Test config overrides
        config_with_custom = self.config.copy()
        config_with_custom['health_server'] = {'host': 'localhost', 'port': 9999}
        hs3 = HealthServer(config_with_custom)
        self.assertEqual(hs3.host, 'localhost')
        self.assertEqual(hs3.port, 9999)
    
    def test_get_status_basic(self):
        """Test get_status method returns expected keys."""
        from src.health_server import HealthServer
        
        hs = HealthServer(self.config)
        status = hs.get_status()
        
        # Check required keys
        required_keys = [
            'hostname', 'ip_address', 'kernel_version', 'system', 'machine',
            'python_version', 'start_time', 'uptime', 'uptime_seconds',
            'cache_size', 'data_size', 'log_size', 'disk_usage', 'memory_usage',
            'stock_count', 'monitored_stocks', 'last_run_time', 'next_run_time'
        ]
        
        for key in required_keys:
            self.assertIn(key, status, f"Missing key in status: {key}")
        
        # Check specific values
        self.assertEqual(status['stock_count'], 3)
        self.assertEqual(status['monitored_stocks'], ['601728', '600938', '601390'])
        self.assertIn('Linux', status['system'])  # Assuming Linux
        self.assertIsInstance(status['uptime'], str)
        self.assertIsInstance(status['uptime_seconds'], int)
    
    @patch('socket.gethostname')
    @patch('socket.gethostbyname_ex')
    @patch('platform.release')
    @patch('platform.system')
    @patch('platform.machine')
    def test_get_status_with_mocks(self, mock_machine, mock_system, 
                                   mock_release, mock_gethostbyname_ex, 
                                   mock_gethostname):
        """Test get_status with mocked system calls."""
        from src.health_server import HealthServer
        
        # Setup mocks
        mock_gethostname.return_value = 'test-server'
        mock_gethostbyname_ex.return_value = ('test-server', [], ['192.168.1.100', '10.0.0.1'])
        mock_release.return_value = '5.15.0-60-generic'
        mock_system.return_value = 'Linux'
        mock_machine.return_value = 'x86_64'
        
        hs = HealthServer(self.config)
        status = hs.get_status()
        
        self.assertEqual(status['hostname'], 'test-server')
        self.assertEqual(status['ip_address'], '192.168.1.100, 10.0.0.1')
        self.assertEqual(status['kernel_version'], '5.15.0-60-generic')
        self.assertEqual(status['system'], 'Linux')
        self.assertEqual(status['machine'], 'x86_64')
    
    def test_format_uptime(self):
        """Test _format_uptime method."""
        from src.health_server import HealthServer
        
        hs = HealthServer(self.config)
        
        test_cases = [
            (0, "0秒"),
            (30, "30秒"),
            (90, "1分钟 30秒"),
            (3600, "1小时 0分钟 0秒"),
            (3660, "1小时 1分钟 0秒"),
            (86400, "1天 0小时 0分钟"),
            (90000, "1天 1小时 0分钟"),
            (90060, "1天 1小时 1分钟")
        ]
        
        for seconds, expected in test_cases:
            result = hs._format_uptime(seconds)
            self.assertEqual(result, expected)
    
    def test_directory_size_methods(self):
        """Test directory size calculation methods."""
        from src.health_server import HealthServer
        
        hs = HealthServer(self.config)
        
        # Create test files
        test_dir = os.path.join(self.temp_dir, 'test_dir')
        os.makedirs(test_dir, exist_ok=True)
        
        # Create a 1KB file
        test_file = os.path.join(test_dir, 'test.txt')
        with open(test_file, 'wb') as f:
            f.write(b'x' * 1024)  # 1KB
        
        # Test _get_directory_size_bytes
        size_bytes = hs._get_directory_size_bytes(Path(test_dir))
        self.assertEqual(size_bytes, 1024)
        
        # Test _get_directory_size (human readable)
        size_human = hs._get_directory_size(Path(test_dir))
        self.assertIn('KB', size_human)  # Should be "1.0 KB"
    
    @patch('socketserver.TCPServer')
    def test_start_server(self, mock_tcpserver):
        """Test start method with mocked server."""
        from src.health_server import HealthServer
        
        # Mock TCPServer
        mock_server_instance = MagicMock()
        mock_tcpserver.return_value = mock_server_instance
        
        hs = HealthServer(self.config)
        
        # Test starting as daemon
        result = hs.start(daemon=True)
        self.assertTrue(result)
        mock_tcpserver.assert_called_once()
        
        # Test server thread start
        self.assertIsNotNone(hs.thread)
        self.assertTrue(hs.thread.daemon)
        
        # Test stopping server
        hs.stop()
        mock_server_instance.shutdown.assert_called_once()
        mock_server_instance.server_close.assert_called_once()
    
    def test_calculate_next_run_time(self):
        """Test _calculate_next_run_time method."""
        from src.health_server import HealthServer
        
        hs = HealthServer(self.config)
        
        # Test with default run time (15:30)
        next_run = hs._calculate_next_run_time()
        self.assertIsInstance(next_run, str)
        # Should be in format YYYY-MM-DD HH:MM:SS
        import re
        self.assertTrue(re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', next_run))
    
    def test_health_handler_creation(self):
        """Test HealthHandler can be instantiated."""
        from src.health_server import HealthHandler, HealthServer
        
        hs = HealthServer(self.config)
        
        # Mock request and client address
        mock_request = MagicMock()
        mock_client_address = ('127.0.0.1', 12345)
        mock_server = MagicMock()
        
        # Create handler with health_server instance
        handler = HealthHandler(mock_request, mock_client_address, mock_server, 
                                health_server=hs)
        
        self.assertIsNotNone(handler)
        self.assertEqual(handler.health_server, hs)

if __name__ == '__main__':
    unittest.main()