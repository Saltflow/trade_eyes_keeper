#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试配置加载和环境变量覆盖
"""
import os
import sys
import tempfile
import yaml
from pathlib import Path

def test_config_loading_without_env():
    """测试无环境变量时的配置加载（应使用配置文件中的占位符）"""
    # 创建临时配置文件
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / 'config.yaml'
        config_content = {
            'llm': {'api_key': 'placeholder_llm_key'},
            'email': {
                'sender_email': 'test@example.com',
                'sender_password': 'placeholder_password',
                'receiver_email': 'receiver@example.com'
            }
        }
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_content, f)
        
        # 临时修改环境变量，确保没有相关环境变量
        env_backup = {}
        for key in ['DEEPSEEK_API_KEY', 'EMAIL_SENDER', 'EMAIL_PASSWORD', 'EMAIL_RECEIVER']:
            env_backup[key] = os.environ.get(key)
            os.environ.pop(key, None)
        
        try:
            # 加载配置（需要模拟load_config从指定路径加载）
            # 由于load_config使用固定路径，我们暂时直接测试配置读取
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            assert config['llm']['api_key'] == 'placeholder_llm_key'
            assert config['email']['sender_email'] == 'test@example.com'
            assert config['email']['sender_password'] == 'placeholder_password'
        finally:
            # 恢复环境变量
            for key, value in env_backup.items():
                if value is not None:
                    os.environ[key] = value

def test_config_loading_with_env():
    """测试环境变量覆盖配置"""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / 'config.yaml'
        config_content = {
            'llm': {'api_key': 'placeholder_llm_key'},
            'email': {
                'sender_email': 'test@example.com',
                'sender_password': 'placeholder_password',
                'receiver_email': 'receiver@example.com'
            }
        }
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_content, f)
        
        # 设置环境变量
        os.environ['DEEPSEEK_API_KEY'] = 'real_api_key_from_env'
        os.environ['EMAIL_PASSWORD'] = 'real_password_from_env'
        
        # 模拟load_config的逻辑：加载YAML然后用环境变量覆盖
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 应用环境变量覆盖（模拟main.py中的逻辑）
        if os.getenv('EMAIL_SENDER'):
            config.setdefault('email', {})['sender_email'] = os.getenv('EMAIL_SENDER')
        if os.getenv('EMAIL_PASSWORD'):
            config.setdefault('email', {})['sender_password'] = os.getenv('EMAIL_PASSWORD')
        if os.getenv('EMAIL_RECEIVER'):
            config.setdefault('email', {})['receiver_email'] = os.getenv('EMAIL_RECEIVER')
        if os.getenv('DEEPSEEK_API_KEY'):
            config.setdefault('llm', {})['api_key'] = os.getenv('DEEPSEEK_API_KEY')
        
        # 验证环境变量已覆盖
        assert config['llm']['api_key'] == 'real_api_key_from_env'
        assert config['email']['sender_password'] == 'real_password_from_env'
        # 未设置的环境变量应保持原样
        assert config['email']['sender_email'] == 'test@example.com'
        
        # 清理环境变量
        del os.environ['DEEPSEEK_API_KEY']
        del os.environ['EMAIL_PASSWORD']

def test_no_chinese_placeholders_in_env():
    """检查环境变量中没有中文字符占位符"""
    # 这个测试假设环境变量已通过.env文件设置
    # 我们只检查是否存在中文字符（可能表示占位符未被正确替换）
    def contains_chinese(text):
        if not text:
            return False
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False
    
    # 检查关键环境变量
    for key in ['DEEPSEEK_API_KEY', 'EMAIL_PASSWORD', 'EMAIL_SENDER', 'EMAIL_RECEIVER']:
        value = os.getenv(key)
        if value:
            assert not contains_chinese(value), f"环境变量 {key} 包含中文字符，可能是占位符"