#!/usr/bin/env python3
"""
测试LLM分析器
"""
import sys
import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env')
load_dotenv(env_path)

# Add project root to Python path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

# 设置日志
logging.basicConfig(level=logging.INFO)

from src.llm_analyzer import LLMAnalyzer

def test_llm_analyzer():
    """测试LLM分析器"""
    api_key = os.getenv('DEEPSEEK_API_KEY')
    if not api_key:
        print("错误: 未设置DEEPSEEK_API_KEY环境变量")
        return
    
    config = {
        'llm': {
            'api_key': api_key,
            'base_url': 'https://api.deepseek.com/v1',
            'model': 'deepseek-chat'
        }
    }
    
    print("测试LLM分析器...")
    print("=" * 60)
    
    analyzer = LLMAnalyzer(config)
    
    if analyzer.client is None:
        print("LLM客户端未初始化，检查API密钥配置")
        return
    
    print("LLM客户端初始化成功")
    
    # 测试简单分析
    stock_codes = ['601728']  # 只测试一个股票以减少API调用
    
    print(f"分析股票 {stock_codes}...")
    
    try:
        result = analyzer.analyze_stocks(stock_codes)
        
        if result:
            print("分析成功!")
            print(f"结果类型: {type(result)}")
            
            if isinstance(result, dict):
                print(f"结果键: {list(result.keys())}")
                
                for stock_code, analysis in result.items():
                    print(f"\n股票 {stock_code} 分析:")
                    if isinstance(analysis, dict):
                        for key, value in analysis.items():
                            print(f"  {key}: {value}")
                    else:
                        print(f"  {analysis}")
            else:
                print(f"分析结果: {result}")
        else:
            print("分析返回空结果")
            
    except Exception as e:
        print(f"分析失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("测试完成")

if __name__ == "__main__":
    test_llm_analyzer()