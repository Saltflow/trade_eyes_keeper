#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报分析器模块
使用LLM分析上市公司财务报告，重点关注成本结构、利润变化和资产清算价值
参考橡树资本马克斯的投资理念，从审计角度分析支出变化合理性
"""

import logging
import json
import re
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from .base import BaseLLMClient

logger = logging.getLogger(__name__)


class FinancialReportAnalyzer(BaseLLMClient):
    """
    LLM财报分析器
    专注于上市公司财务报告分析，包括：
    1. 成本结构和同比变化（审计角度切入支出变化合理性）
    2. 利润变化和竞争力评估
    3. 资产清算价值计算（参考橡树资本马克斯的观点）
    """

    def analyze_financial_report(
        self,
        stock_code: str,
        report_text: str,
        report_type: str,
        period_date: str,
        report_title: str = "",
    ) -> Dict[str, Any]:
        """
        分析财务报告文本，提取关键财务数据并进行多维度分析

        Args:
            stock_code: 股票代码
            report_text: 财报正文文本（PDF/HTML提取内容）
            report_type: 报告类型 ('annual', 'semiannual', 'quarterly')
            period_date: 报告期间（如 '2024-12-31'）
            report_title: 报告标题（可选）

        Returns:
            dict: 分析结果，包含提取的财务数据和多维分析
        """
        if not hasattr(self, "api_key") or not self.api_key:
            logger.warning("LLM API密钥未配置，跳过财报分析")
            return {"success": False, "error": "LLM API未配置"}

        # 生成缓存键：股票代码+报告类型+期间+内容哈希（暂不使用缓存）
        content_hash = self._calculate_content_hash(report_text)
        cache_key = f"financial_report_analysis:{stock_code}:{report_type}:{period_date}:{content_hash}"

        logger.info(
            f"开始分析财报: {stock_code} {report_type} {period_date} ({len(report_text)} chars)"
        )

        try:
            # 多步骤LLM分析流程
            analysis_result = self._multi_step_financial_analysis(
                stock_code, report_text, report_type, period_date, report_title
            )

            # 暂不缓存，后续可添加缓存逻辑
            # if analysis_result.get("success", False):
            #     # 缓存分析结果
            #     pass

            return analysis_result

        except Exception as e:
            logger.error(f"财报分析失败: {stock_code} {report_type} {period_date}: {e}")
            return {
                "success": False,
                "error": f"分析失败: {e}",
                "stock_code": stock_code,
                "report_type": report_type,
                "period_date": period_date,
            }

    def _multi_step_financial_analysis(
        self,
        stock_code: str,
        report_text: str,
        report_type: str,
        period_date: str,
        report_title: str,
    ) -> Dict[str, Any]:
        """
        多步骤财务分析流程（5-10步LLM调用）
        步骤:
        1. 提取关键财务数据表格（收入、成本、利润、资产、负债等）
        2. 分析成本结构及同比变化（审计角度）
        3. 分析利润变化及竞争力评估
        4. 计算资产清算价值（橡树资本方法）
        5. 生成审计风险提示
        6. 综合评估与投资建议
        """
        result = {
            "success": True,
            "stock_code": stock_code,
            "report_type": report_type,
            "period_date": period_date,
            "report_title": report_title,
            "analysis_steps": {},
            "extracted_financial_data": None,
            "cost_structure_analysis": None,
            "profit_competitiveness_analysis": None,
            "liquidation_value_analysis": None,
            "audit_risk_insights": None,
            "overall_assessment": None,
        }

        try:
            # 步骤1: 提取关键财务数据
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤1: 提取关键财务数据 - {stock_code}")
            financial_data = self._extract_financial_data(
                stock_code, report_text, report_type, period_date
            )
            result["extracted_financial_data"] = financial_data
            result["analysis_steps"]["financial_data_extraction"] = "completed"

            if not financial_data.get("success", False):
                result["success"] = False
                result["error"] = financial_data.get("error", "财务数据提取失败")
                return result

            # 步骤2: 分析成本结构及同比变化
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤2: 分析成本结构 - {stock_code}")
            cost_analysis = self._analyze_cost_structure(financial_data)
            result["cost_structure_analysis"] = cost_analysis
            result["analysis_steps"]["cost_structure_analysis"] = "completed"

            # 步骤3: 分析利润变化及竞争力
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤3: 分析利润变化及竞争力 - {stock_code}")
            profit_analysis = self._analyze_profit_changes(financial_data)
            result["profit_competitiveness_analysis"] = profit_analysis
            result["analysis_steps"]["profit_competitiveness_analysis"] = "completed"

            # 步骤4: 计算资产清算价值
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤4: 计算资产清算价值 - {stock_code}")
            liquidation_analysis = self._calculate_liquidation_value(financial_data)
            result["liquidation_value_analysis"] = liquidation_analysis
            result["analysis_steps"]["liquidation_value_analysis"] = "completed"

            # 步骤5: 生成审计风险提示
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤5: 生成审计风险提示 - {stock_code}")
            audit_insights = self._generate_audit_insights(financial_data)
            result["audit_risk_insights"] = audit_insights
            result["analysis_steps"]["audit_risk_insights"] = "completed"

            # 步骤6: 综合评估
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤6: 综合评估 - {stock_code}")
            overall_assessment = self._generate_overall_assessment(
                financial_data,
                cost_analysis,
                profit_analysis,
                liquidation_analysis,
                audit_insights,
            )
            result["overall_assessment"] = overall_assessment
            result["analysis_steps"]["overall_assessment"] = "completed"

            # 汇总结果
            result["analysis_completed"] = True
            result["llm_calls_used"] = self._llm_calls_made
            logger.info(
                f"财报分析完成: {stock_code} {report_type} {period_date}, 使用LLM调用: {self._llm_calls_made}"
            )

        except Exception as e:
            logger.error(f"多步骤分析过程中出错: {e}")
            result["success"] = False
            result["error"] = f"分析过程出错: {e}"

        return result

    def _extract_financial_data(
        self, stock_code: str, report_text: str, report_type: str, period_date: str
    ) -> Dict[str, Any]:
        """
        从财报文本中提取关键财务数据表格
        使用LLM识别并结构化财务数据
        """
        # 构建提示词
        prompt = f"""你是一名专业的财务分析师，请从以下上市公司财务报告中提取关键财务数据。

股票代码: {stock_code}
报告类型: {report_type}
报告期间: {period_date}

请提取以下关键财务指标（如果存在）：
1. 收入类：营业收入、营业总收入、主营业务收入
2. 成本类：营业成本、销售费用、管理费用、研发费用、财务费用
3. 利润类：营业利润、利润总额、净利润、归母净利润、扣非净利润
4. 资产类：总资产、流动资产、非流动资产、货币资金、应收账款、存货
5. 负债类：总负债、流动负债、非流动负债、短期借款、长期借款
6. 权益类：所有者权益（净资产）、归母净资产
7. 现金流量：经营活动现金流净额、投资活动现金流净额、筹资活动现金流净额

请按以下格式返回JSON数据，仅包含提取到的数据：
{{
  "success": true/false,
  "financial_data": {{
    "income_statement": {{
      "revenue": 金额（万元）,
      "cost_of_revenue": 金额（万元）,
      "gross_profit": 金额（万元）,
      "operating_expenses": {{
        "sales_expenses": 金额（万元）,
        "management_expenses": 金额（万元）,
        "rd_expenses": 金额（万元）,
        "financial_expenses": 金额（万元）
      }},
      "operating_profit": 金额（万元）,
      "total_profit": 金额（万元）,
      "net_profit": 金额（万元）,
      "net_profit_attributable": 金额（万元）
    }},
    "balance_sheet": {{
      "total_assets": 金额（万元）,
      "current_assets": 金额（万元）,
      "non_current_assets": 金额（万元）,
      "total_liabilities": 金额（万元）,
      "current_liabilities": 金额（万元）,
      "non_current_liabilities": 金额（万元）,
      "equity": 金额（万元）
    }},
    "cash_flow": {{
      "operating_cash_flow": 金额（万元）,
      "investing_cash_flow": 金额（万元）,
      "financing_cash_flow": 金额（万元）
    }}
  }},
  "extraction_confidence": "high/medium/low",
  "notes": "提取过程中的备注"
}}

财报文本内容（可能不完整）：
{report_text[:10000]}  # 限制文本长度
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名专业的财务分析师，擅长从财务报告中提取结构化数据。请严格按JSON格式返回结果。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.3, max_tokens=4000
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 尝试解析JSON响应
            try:
                import json

                extracted_data = json.loads(llm_response)
                if isinstance(extracted_data, dict):
                    extracted_data["llm_response_raw"] = llm_response[:1000]
                    return extracted_data
                else:
                    return {
                        "success": False,
                        "error": "LLM返回非JSON格式",
                        "llm_response": llm_response[:1000],
                    }
            except json.JSONDecodeError:
                # 尝试从文本中提取JSON
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        extracted_data = json.loads(json_match.group())
                        extracted_data["llm_response_raw"] = llm_response[:1000]
                        return extracted_data
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析LLM返回的JSON",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"提取财务数据时出错: {e}")
            return {"success": False, "error": f"提取失败: {e}"}

    def _analyze_cost_structure(self, financial_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析成本结构及同比变化（审计角度）
        关注支出变化的合理性
        """
        prompt = f"""你是一名审计专家，请分析以下财务数据的成本结构：

财务数据:
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

请从审计角度分析：
1. 成本构成比例（营业成本、销售费用、管理费用、研发费用、财务费用占总收入的比例）
2. 各项费用的同比变化趋势（如果数据包含多期，否则分析绝对水平）
3. 费用变化的合理性评估：
   - 销售费用增长是否与收入增长匹配？
   - 研发费用是否与公司战略一致？
   - 财务费用是否反映合理的融资成本？
4. 潜在审计风险点：
   - 异常的费用波动
   - 不匹配的成本收入关系
   - 可能的费用资本化或递延处理

请按以下JSON格式返回分析结果：
{{
  "success": true/false,
  "cost_structure_analysis": {{
    "cost_ratios": {{
      "cost_of_revenue_to_revenue": 比例,
      "sales_expenses_to_revenue": 比例,
      "management_expenses_to_revenue": 比例,
      "rd_expenses_to_revenue": 比例,
      "financial_expenses_to_revenue": 比例
    }},
    "change_assessment": "合理/部分合理/不合理",
    "key_findings": ["发现1", "发现2"],
    "audit_risks": ["风险1", "风险2"],
    "recommendations": ["建议1", "建议2"]
  }},
  "analysis_confidence": "high/medium/low"
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名经验丰富的审计专家，擅长发现财务数据中的异常和风险点。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.4, max_tokens=3000
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"成本结构分析时出错: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

    def _analyze_profit_changes(self, financial_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析利润变化及竞争力评估
        """
        prompt = f"""你是一名投资分析师，请分析以下财务数据的利润变化和公司竞争力：

财务数据:
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

请分析：
1. 盈利能力指标：
   - 毛利率、净利率、营业利润率
   - ROE（净资产收益率）、ROA（总资产收益率）
2. 利润变化趋势（如果有多期数据）
3. 竞争力评估：
   - 与行业平均水平比较
   - 核心竞争优势分析
   - 盈利可持续性评估
4. 投资价值点：
   - 利润增长驱动力
   - 潜在增长空间
   - 风险因素

请按以下JSON格式返回分析结果：
{{
  "success": true/false,
  "profit_analysis": {{
    "profitability_ratios": {{
      "gross_margin": 比例,
      "net_margin": 比例,
      "operating_margin": 比例,
      "roe": 比例,
      "roa": 比例
    }},
    "trend_assessment": "改善/稳定/恶化",
    "competitive_position": "强/中/弱",
    "growth_drivers": ["驱动因素1", "驱动因素2"],
    "risk_factors": ["风险因素1", "风险因素2"],
    "investment_conclusion": "积极/中性/谨慎"
  }}
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名资深投资分析师，擅长评估公司盈利能力和竞争力。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.4, max_tokens=3000
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"利润变化分析时出错: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

    def _calculate_liquidation_value(
        self, financial_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        计算资产清算价值（参考橡树资本马克斯的观点）
        关注安全边际和资产变现能力
        """
        prompt = f"""你是一名价值投资分析师，参考橡树资本霍华德·马克斯的投资理念，
计算以下公司的资产清算价值：

财务数据:
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

请计算：
1. 资产变现价值（保守估计）：
   - 流动资产变现率（货币资金100%，应收账款80%，存货50%等）
   - 非流动资产变现率（固定资产60%，无形资产20%等）
2. 负债清偿优先级
3. 清算价值计算：
   - 总可变现资产
   - 减：优先清偿负债
   - 股东可获得的清算价值
4. 安全边际分析：
   - 清算价值与市值的比较
   - 破产风险评估
5. 橡树资本风格建议：
   - 是否具有足够的安全边际
   - 投资时机建议

请按以下JSON格式返回分析结果：
{{
  "success": true/false,
  "liquidation_analysis": {{
    "asset_liquidation_values": {{
      "cash_equivalents": 金额（万元）,
      "receivables_liquidation": 金额（万元）,
      "inventory_liquidation": 金额（万元）,
      "fixed_assets_liquidation": 金额（万元）
    }},
    "total_liquidation_value": 金额（万元）,
    "priority_liabilities": 金额（万元）,
    "shareholder_liquidation_value": 金额（万元）,
    "safety_margin": "高/中/低",
    "bankruptcy_risk": "低/中/高",
    "oaktree_style_recommendation": "推荐/中性/避免"
  }}
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名价值投资专家，擅长计算公司清算价值和评估安全边际。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.3, max_tokens=3000
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"清算价值计算时出错: {e}")
            return {"success": False, "error": f"计算失败: {e}"}

    def _generate_audit_insights(
        self, financial_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成审计风险提示（审计角度）
        """
        prompt = f"""你是一名审计合伙人，请对以下财务数据提供审计风险提示：

财务数据:
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

请关注：
1. 重大审计风险领域
2. 需要特别审计程序的项目
3. 可能的会计操纵迹象
4. 内部控制薄弱环节
5. 审计意见类型建议

请按以下JSON格式返回：
{{
  "success": true/false,
  "audit_insights": {{
    "high_risk_areas": ["领域1", "领域2"],
    "special_audit_procedures": ["程序1", "程序2"],
    "potential_manipulation_indicators": ["迹象1", "迹象2"],
    "internal_control_weaknesses": ["弱点1", "弱点2"],
    "audit_opinion_recommendation": "无保留意见/带强调事项段/保留意见/否定意见/无法表示意见"
  }}
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名审计合伙人，擅长识别财务审计风险和会计操纵迹象。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.4, max_tokens=2500
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"审计洞察生成时出错: {e}")
            return {"success": False, "error": f"生成失败: {e}"}

    def _generate_overall_assessment(
        self,
        financial_data: Dict[str, Any],
        cost_analysis: Dict[str, Any],
        profit_analysis: Dict[str, Any],
        liquidation_analysis: Dict[str, Any],
        audit_insights: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        生成综合评估与投资建议
        """
        prompt = f"""作为首席投资官，请基于以下多维度分析结果，提供综合评估：

1. 财务数据:
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

2. 成本结构分析:
{json.dumps(cost_analysis, indent=2, ensure_ascii=False)}

3. 利润竞争力分析:
{json.dumps(profit_analysis, indent=2, ensure_ascii=False)}

4. 清算价值分析:
{json.dumps(liquidation_analysis, indent=2, ensure_ascii=False)}

5. 审计风险提示:
{json.dumps(audit_insights, indent=2, ensure_ascii=False)}

请提供：
1. 综合评分（1-10分）
2. 主要优势
3. 主要风险
4. 投资建议（买入/持有/卖出）
5. 投资期限建议（短期/中期/长期）
6. 关键监控指标

请按以下JSON格式返回：
{{
  "success": true/false,
  "overall_assessment": {{
    "composite_score": 分数（1-10）,
    "strengths": ["优势1", "优势2"],
    "risks": ["风险1", "风险2"],
    "investment_recommendation": "买入/持有/卖出",
    "investment_horizon": "短期/中期/长期",
    "key_monitoring_metrics": ["指标1", "指标2"],
    "summary": "综合评估摘要"
  }}
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名首席投资官，擅长综合多维度分析做出投资决策。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.5, max_tokens=3500
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        return json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"综合评估生成时出错: {e}")
            return {"success": False, "error": f"生成失败: {e}"}

    def _calculate_content_hash(self, content: str) -> str:
        """计算内容哈希值用于缓存"""
        import hashlib

        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def analyze_multiple_reports(
        self,
        stock_code: str,
        reports: List[Tuple[str, str, str, str]],
        max_concurrent: int = 3,
    ) -> Dict[str, Any]:
        """
        分析同一公司的多期财报，进行趋势分析

        Args:
            stock_code: 股票代码
            reports: 报告列表，每个元素为 (report_text, report_type, period_date, report_title)
            max_concurrent: 最大并发分析数量

        Returns:
            dict: 多期分析结果，包含趋势分析
        """
        logger.info(f"开始分析 {stock_code} 的 {len(reports)} 期财报")

        individual_results = []
        for i, (report_text, report_type, period_date, report_title) in enumerate(
            reports
        ):
            logger.info(
                f"分析第 {i + 1}/{len(reports)} 期: {report_type} {period_date}"
            )
            result = self.analyze_financial_report(
                stock_code, report_text, report_type, period_date, report_title
            )
            individual_results.append(
                {
                    "period": period_date,
                    "type": report_type,
                    "result": result,
                }
            )

        # 趋势分析（简化版）
        trend_analysis = self._analyze_financial_trends(individual_results)

        return {
            "success": True,
            "stock_code": stock_code,
            "individual_reports": individual_results,
            "trend_analysis": trend_analysis,
            "total_reports_analyzed": len(individual_results),
        }

    def _analyze_financial_trends(
        self, individual_results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """分析多期财报趋势"""
        # 简化实现，实际可扩展
        return {
            "success": True,
            "trend_summary": "多期财报趋势分析（待完善）",
            "periods_covered": len(individual_results),
        }
