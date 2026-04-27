#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财务报告分析器测试
"""

import pytest
import json
from unittest.mock import Mock, patch
from src.analysis.llm_analyzer import FinancialReportAnalyzer


class TestFinancialReportAnalyzer:
    """财务报告分析器测试类"""

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        return {
            "llm": {"api_key": "test-key", "base_url": "https://api.test.com/v1"},
            "announcements": {"max_llm_calls_per_run": 10},
            "storage": {"cache_dir": "./cache"},
        }

    @pytest.fixture
    def analyzer(self, mock_config):
        """创建分析器实例"""
        return FinancialReportAnalyzer(mock_config)

    def test_initialization(self, analyzer):
        """测试分析器初始化"""
        assert analyzer is not None
        assert hasattr(analyzer, "api_key")
        assert analyzer.api_key == "test-key"
        assert analyzer.max_llm_calls_per_run == 10

    def test_analyze_financial_report_without_api_key(self):
        """测试没有API密钥时的分析"""
        config = {"llm": {}, "announcements": {}, "storage": {}}
        analyzer = FinancialReportAnalyzer(config)
        result = analyzer.analyze_financial_report(
            "000001", "测试财报文本", "annual", "2024-12-31"
        )
        assert result["success"] is False
        assert "LLM API未配置" in result.get("error", "")

    @patch.object(FinancialReportAnalyzer, "_call_chat_completion")
    def test_extract_financial_data_success(self, mock_llm_call, analyzer):
        """测试成功提取财务数据"""
        # 模拟LLM响应
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "success": True,
                                "financial_data": {
                                    "income_statement": {
                                        "revenue": 1000000.0,
                                        "cost_of_revenue": 600000.0,
                                        "gross_profit": 400000.0,
                                    },
                                    "balance_sheet": {
                                        "total_assets": 5000000.0,
                                        "total_liabilities": 3000000.0,
                                        "equity": 2000000.0,
                                    },
                                    "cash_flow": {
                                        "operating_cash_flow": 300000.0,
                                        "investing_cash_flow": -100000.0,
                                        "financing_cash_flow": -50000.0,
                                    },
                                },
                                "extraction_confidence": "high",
                                "notes": "提取成功",
                            }
                        )
                    }
                }
            ]
        }
        mock_llm_call.return_value = mock_response

        result = analyzer._extract_financial_data(
            "000001", "财报文本内容", "annual", "2024-12-31"
        )

        assert result["success"] is True
        assert "financial_data" in result
        assert result["financial_data"]["income_statement"]["revenue"] == 1000000.0
        mock_llm_call.assert_called_once()

    @patch.object(FinancialReportAnalyzer, "_call_chat_completion")
    def test_multi_step_analysis_flow(self, mock_llm_call, analyzer):
        """测试多步骤分析流程"""
        # 模拟每一步的LLM响应
        mock_responses = [
            # 步骤1: 提取财务数据
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "financial_data": {
                                        "income_statement": {
                                            "revenue": 1000000.0,
                                            "cost_of_revenue": 600000.0,
                                        }
                                    },
                                    "extraction_confidence": "high",
                                }
                            )
                        }
                    }
                ]
            },
            # 步骤2: 成本结构分析
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "cost_structure_analysis": {
                                        "cost_ratios": {
                                            "cost_of_revenue_to_revenue": 0.6
                                        },
                                        "change_assessment": "合理",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
            # 步骤3: 利润变化分析
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "profit_analysis": {
                                        "profitability_ratios": {"gross_margin": 0.4},
                                        "trend_assessment": "改善",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
            # 步骤4: 清算价值分析
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "liquidation_analysis": {
                                        "total_liquidation_value": 3000000.0,
                                        "safety_margin": "高",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
            # 步骤5: 审计风险提示
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "audit_insights": {
                                        "high_risk_areas": ["无"],
                                        "audit_opinion_recommendation": "无保留意见",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
            # 步骤6: 综合评估
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "success": True,
                                    "overall_assessment": {
                                        "composite_score": 8.5,
                                        "investment_recommendation": "买入",
                                    },
                                }
                            )
                        }
                    }
                ]
            },
        ]

        mock_llm_call.side_effect = mock_responses

        result = analyzer.analyze_financial_report(
            "000001", "财报文本内容", "annual", "2024-12-31"
        )

        assert result["success"] is True
        assert result["analysis_steps"]["financial_data_extraction"] == "completed"
        assert result["analysis_steps"]["cost_structure_analysis"] == "completed"
        assert (
            result["analysis_steps"]["profit_competitiveness_analysis"] == "completed"
        )
        assert result["analysis_steps"]["liquidation_value_analysis"] == "completed"
        assert result["analysis_steps"]["audit_risk_insights"] == "completed"
        assert result["analysis_steps"]["overall_assessment"] == "completed"
        assert mock_llm_call.call_count == 6

    def test_calculate_content_hash(self, analyzer):
        """测试内容哈希计算"""
        content = "测试财报内容"
        hash1 = analyzer._calculate_content_hash(content)
        hash2 = analyzer._calculate_content_hash(content)
        assert hash1 == hash2
        assert len(hash1) == 32  # MD5哈希长度

    def test_financial_report_fetcher_integration(self):
        """测试财报获取器集成（简化）"""
        # 导入财报获取器
        from src.analysis.financial_report_fetcher import FinancialReportFetcher

        config = {
            "llm": {"api_key": "test-key"},
            "announcements": {"max_llm_calls_per_run": 10},
            "storage": {"cache_dir": "./cache"},
        }

        # 创建模拟的公告获取器和内容获取器
        mock_announcement_fetcher = Mock()
        mock_content_fetcher = Mock()

        fetcher = FinancialReportFetcher(
            config, mock_announcement_fetcher, mock_content_fetcher
        )

        assert fetcher.config == config
        assert fetcher.announcement_fetcher == mock_announcement_fetcher
        assert fetcher.content_fetcher == mock_content_fetcher

    @patch.object(FinancialReportAnalyzer, "_call_chat_completion")
    def test_llm_call_limit(self, mock_llm_call, analyzer):
        """测试LLM调用限制"""
        # 设置最大调用次数为2
        analyzer.max_llm_calls_per_run = 2
        analyzer._llm_calls_made = 0

        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "success": True,
                                "financial_data": {
                                    "income_statement": {"revenue": 1000}
                                },
                            }
                        )
                    }
                }
            ]
        }
        mock_llm_call.return_value = mock_response

        # 第一次调用应该成功
        result1 = analyzer._extract_financial_data(
            "000001", "文本1", "annual", "2024-12-31"
        )
        assert result1["success"] is True
        assert analyzer._llm_calls_made == 1

        # 第二次调用应该成功
        result2 = analyzer._extract_financial_data(
            "000001", "文本2", "annual", "2024-12-31"
        )
        assert result2["success"] is True
        assert analyzer._llm_calls_made == 2

        # 第三次调用应该失败（达到限制）
        # 注意：_can_make_llm_call 会检查限制
        analyzer._llm_calls_made = 2  # 手动设置计数器
        # 在分析财报时，_multi_step_financial_analysis会检查限制
        # 我们直接测试_can_make_llm_call
        assert analyzer._can_make_llm_call() is False
