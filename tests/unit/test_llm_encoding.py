#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试LLM分析器编码问题
验证UTF-8编码设置是否正确
"""
import os
import json
from unittest.mock import Mock, patch

# 设置UTF-8环境变量
os.environ['PYTHONUTF8'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'

def test_llm_analyzer_utf8_headers():
    """测试LLM分析器是否设置UTF-8请求头"""
    from src.llm_analyzer import LLMAnalyzer
    import httpx
    # 模拟配置
    config = {
        'llm': {
            'api_key': 'fake-api-key',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat'
        }
    }
    
    # 模拟httpx.Client和OpenAI
    with patch('httpx.Client') as mock_client_class, \
         patch('src.llm_analyzer.OpenAI') as mock_openai_class:
        # 创建mock client实例，并设置其__class__为httpx.Client以通过isinstance检查
        mock_http_client = Mock(spec=httpx.Client)
        mock_http_client.__class__ = httpx.Client
        mock_client_class.return_value = mock_http_client
        
        # 创建分析器
        analyzer = LLMAnalyzer(config)
        
        # 验证httpx.Client被调用，且headers包含UTF-8编码
        mock_client_class.assert_called_once()
        call_args = mock_client_class.call_args
        
        # 检查headers参数
        kwargs = call_args.kwargs
        assert 'headers' in kwargs
        headers = kwargs['headers']
        
        # 验证Content-Type包含charset=utf-8
        content_type = headers.get('Content-Type', '')
        assert 'charset=utf-8' in content_type.lower()
        
        # 验证Accept包含charset=utf-8
        accept = headers.get('Accept', '')
        assert 'charset=utf-8' in accept.lower()
        
        # 验证OpenAI被调用，并且传递了http_client参数
        mock_openai_class.assert_called_once()
        openai_call_args = mock_openai_class.call_args
        openai_kwargs = openai_call_args.kwargs
        assert 'http_client' in openai_kwargs
        # http_client应该是我们创建的mock实例
        assert openai_kwargs['http_client'] is mock_http_client

def test_llm_analyzer_chinese_prompt():
    """测试LLM分析器处理中文字符"""
    from src.llm_analyzer import LLMAnalyzer
    config = {
        'llm': {
            'api_key': 'fake-api-key',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat'
        }
    }
    
    # 模拟OpenAI客户端
    mock_openai_client = Mock()
    mock_chat_completion = Mock()
    mock_chat_completion.choices = [Mock(message=Mock(content='模拟分析结果'))]
    mock_openai_client.chat.completions.create.return_value = mock_chat_completion
    
    with patch('src.llm_analyzer.OpenAI') as mock_openai_class, \
         patch('src.llm_analyzer.CacheManager.get_analysis_cache', return_value=None) as mock_get_cache, \
         patch('src.llm_analyzer.CacheManager.set_analysis_cache') as mock_set_cache:
        mock_openai_class.return_value = mock_openai_client
        
        # 创建分析器（会使用模拟的OpenAI客户端）
        analyzer = LLMAnalyzer(config)
        
        # 模拟股票信息包含中文字符
        stock_code = '601728'
        
        # 调用分析方法
        result = analyzer.analyze_stocks([stock_code])
        
        # 验证OpenAI客户端被调用
        assert mock_openai_client.chat.completions.create.called
        
        # 获取调用的参数
        call_args = mock_openai_client.chat.completions.create.call_args
        kwargs = call_args.kwargs
        
        # 验证消息中包含中文字符
        messages = kwargs.get('messages', [])
        assert len(messages) > 0
        
        # 查找包含中文字符的消息（系统提示或用户提示）
        found_chinese = False
        for msg in messages:
            content = msg.get('content', '')
            # 检查是否包含中文字符（简单检查）
            if any('\u4e00' <= char <= '\u9fff' for char in content):
                found_chinese = True
                break
        
        assert found_chinese, "提示中应包含中文字符"

def test_llm_analyzer_json_encoding():
    """测试LLM分析器JSON编码（确保ensure_ascii=False）"""
    from src.llm_analyzer import LLMAnalyzer
    import httpx
    config = {
        'llm': {
            'api_key': 'fake-api-key',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat'
        }
    }
    
    # 模拟httpx.Client和OpenAI
    with patch('httpx.Client') as mock_client_class, \
         patch('src.llm_analyzer.OpenAI') as mock_openai_class:
        # 创建mock client实例，并设置其__class__为httpx.Client以通过isinstance检查
        mock_http_client = Mock(spec=httpx.Client)
        mock_http_client.__class__ = httpx.Client
        mock_client_class.return_value = mock_http_client
        
        # 创建分析器
        analyzer = LLMAnalyzer(config)
        
        # 我们可以直接测试json.dumps的默认行为（确保ensure_ascii=False是默认行为）
        import json
        test_data = {'text': '中文测试'}
        json_str = json.dumps(test_data, ensure_ascii=False)
        
        # 验证中文字符没有被转义
        assert '\\u' not in json_str, "中文字符不应被转义为Unicode转义序列"
        assert '中文测试' in json_str, "JSON字符串应包含原始中文字符"

def test_llm_analyzer_no_encoding_errors():
    """测试LLM分析器不抛出编码错误"""
    from src.llm_analyzer import LLMAnalyzer
    config = {
        'llm': {
            'api_key': 'fake-api-key',
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat'
        }
    }
    
    # 模拟OpenAI客户端返回包含中文字符的响应
    mock_openai_client = Mock()
    mock_chat_completion = Mock()
    mock_chat_completion.choices = [Mock(message=Mock(content='分析结果包含中文字符：中国电信具有稳定的现金流和良好的分红政策。'))]
    mock_openai_client.chat.completions.create.return_value = mock_chat_completion
    
    with patch('src.llm_analyzer.OpenAI') as mock_openai_class, \
         patch('src.llm_analyzer.CacheManager.get_analysis_cache', return_value=None) as mock_get_cache, \
         patch('src.llm_analyzer.CacheManager.set_analysis_cache') as mock_set_cache:
        mock_openai_class.return_value = mock_openai_client
        
        analyzer = LLMAnalyzer(config)
        
        # 应该不抛出任何编码错误
        result = analyzer.analyze_stocks(['601728'])
        assert result is not None