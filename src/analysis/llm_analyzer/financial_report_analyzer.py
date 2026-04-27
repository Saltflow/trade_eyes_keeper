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
        logger.debug(
            "财报分析入参: stock=%s type=%s period=%s title=%s hash=%s text_len=%s",
            stock_code,
            report_type,
            period_date,
            self._truncate_for_log(report_title or "", 120),
            content_hash,
            len(report_text),
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
            financial_data = self._extract_financial_data_multipass(
                stock_code, report_text, report_type, period_date
            )
            result["extracted_financial_data"] = financial_data
            result["analysis_steps"]["financial_data_extraction"] = "completed"
            self._log_step_output("financial_data_extraction", financial_data)

            if not financial_data.get("success", False):
                result["success"] = False
                result["error"] = financial_data.get("error", "财务数据提取失败")
                logger.warning(
                    "财务数据提取失败，终止后续分析: %s | error=%s",
                    stock_code,
                    financial_data.get("error"),
                )
                return result

            # 最低要求：提取到足够的数值字段，否则短路后续步骤，避免浪费token
            parsed_financial_data = financial_data.get("financial_data", {})
            numeric_fields = self._count_numeric_fields(parsed_financial_data)
            result["numeric_fields_detected"] = numeric_fields
            logger.info(
                "财报数值字段统计: stock=%s fields=%s",
                stock_code,
                numeric_fields,
            )

            # 记录字段名，便于排查哪些数字被解析出来
            try:
                field_names = sorted(list(parsed_financial_data.keys()))
                logger.info(
                    "财报已解析字段: stock=%s count=%s fields=%s",
                    stock_code,
                    numeric_fields,
                    field_names,
                )
                result["numeric_field_names"] = field_names
            except Exception:
                result["numeric_field_names"] = []

            min_required = 15 if report_type in ("annual", "semiannual") else 8

            # 若字段不足，尝试在正文中挖掘数字行补充关键科目，再次计数
            if numeric_fields < min_required:
                mined_fields = self._mine_numeric_from_text(  # type: ignore[attr-defined]
                    report_text
                )
                if mined_fields:
                    parsed_financial_data = {**parsed_financial_data, **mined_fields}
                    financial_data["financial_data"] = parsed_financial_data
                    numeric_fields = self._count_numeric_fields(parsed_financial_data)
                    result["numeric_fields_detected"] = numeric_fields
                    try:
                        field_names = sorted(list(parsed_financial_data.keys()))
                        result["numeric_field_names"] = field_names
                        logger.info(
                            "正文补充后字段: stock=%s count=%s fields=%s",
                            stock_code,
                            numeric_fields,
                            field_names,
                        )
                    except Exception:
                        result["numeric_field_names"] = []

                if numeric_fields < min_required:
                    result["success"] = False
                    result["error"] = (
                        f"结构化数值字段过少({numeric_fields}/{min_required})，疑似文本截断或报表缺失，已短路后续分析"
                    )
                    result["analysis_steps"]["financial_data_extraction"] = "partial"
                    result["short_circuited"] = True
                    self._log_step_output(
                        "financial_data_extraction_insufficient", financial_data
                    )
                    logger.warning(
                        "财报数值字段不足，终止后续分析: %s type=%s detected=%s required=%s",
                        stock_code,
                        report_type,
                        numeric_fields,
                        min_required,
                    )
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
            self._log_step_output("cost_structure_analysis_output", cost_analysis)

            # 步骤3: 分析利润变化及竞争力
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤3: 分析利润变化及竞争力 - {stock_code}")
            profit_analysis = self._analyze_profit_changes(financial_data)
            result["profit_competitiveness_analysis"] = profit_analysis
            result["analysis_steps"]["profit_competitiveness_analysis"] = "completed"
            self._log_step_output("profit_analysis_output", profit_analysis)

            # 步骤4: 计算资产清算价值
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤4: 计算资产清算价值 - {stock_code}")
            liquidation_analysis = self._calculate_liquidation_value(financial_data)
            result["liquidation_value_analysis"] = liquidation_analysis
            result["analysis_steps"]["liquidation_value_analysis"] = "completed"
            self._log_step_output("liquidation_value_output", liquidation_analysis)

            # 步骤5: 生成审计风险提示
            if not self._can_make_llm_call():
                result["success"] = False
                result["error"] = "达到LLM调用限制"
                return result

            logger.info(f"步骤5: 生成审计风险提示 - {stock_code}")
            audit_insights = self._generate_audit_insights(financial_data)
            result["audit_risk_insights"] = audit_insights
            result["analysis_steps"]["audit_risk_insights"] = "completed"
            self._log_step_output("audit_insights_output", audit_insights)

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
            self._log_step_output("overall_assessment_output", overall_assessment)

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

    def _count_numeric_fields(self, data: Any) -> int:
        """统计嵌套字典/列表中的数字字段数量"""

        def _walk(value: Any) -> int:
            if isinstance(value, (int, float)):
                return 1
            if isinstance(value, dict):
                return sum(_walk(v) for v in value.values())
            if isinstance(value, (list, tuple)):
                return sum(_walk(v) for v in value)
            return 0

        return _walk(data)

    def _mine_numeric_from_text(self, report_text: str) -> Dict[str, Any]:
        """从正文挖掘常见科目与比例，补充字段计数（不依赖表格）"""

        # 科目类（金额）
        amount_patterns = {
            "revenue": r"营业收入[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "net_profit": r"(?:归母)?净利润[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "operating_income": r"营业利润[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "total_assets": r"总资产[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "total_liabilities": r"总负债[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "equity": r"(股东权益|所有者权益)[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "current_assets": r"流动资产[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "current_liabilities": r"流动负债[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "cash_and_equivalents": r"货币资金[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "operating_cash_flow": r"经营活动现金流(?:量)?[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "investing_cash_flow": r"投资活动现金流(?:量)?[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "financing_cash_flow": r"筹资活动现金流(?:量)?[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "capex": r"资本(?:性)?支出[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "ebitda": r"EBITDA[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "depreciation": r"折旧[费]?用?[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "amortization": r"摊销[费]?用?[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "interest_expense": r"利息支出[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "ar": r"应收账款[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
            "inventory": r"存货[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)(?:\s*[亿万万元]*)",
        }

        # 比例类（百分比）
        ratio_patterns = {
            "gross_margin": r"毛利率[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
            "net_margin": r"净利率[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
            "operating_margin": r"营业利润率[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
            "asset_liability_ratio": r"资产负债率[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
            "roe": r"ROE[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
            "roa": r"ROA[：:，,。 ]*([0-9]+(?:\.[0-9]+)?)%",
        }

        mined: Dict[str, Any] = {}

        for key, pattern in amount_patterns.items():
            m = re.search(pattern, report_text)
            if m:
                value = self._parse_number_unit(m.group(1), m.group(0))
                if value is not None and key not in mined:
                    mined[key] = value

        for key, pattern in ratio_patterns.items():
            m = re.search(pattern, report_text)
            if m:
                value = self._parse_percent(m.group(1))
                if value is not None and key not in mined:
                    mined[key] = value

        if mined:
            logger.info("正文补充数字字段: %s", mined)
        return mined

    def _parse_number_unit(self, number_str: str, context: str) -> Optional[float]:
        """解析带单位的数字，支持亿/万/元；无法解析返回None"""

        try:
            num = float(number_str)
        except Exception:
            return None

        context_lower = context.lower()
        if "亿" in context_lower:
            return num * 1e8
        if "万" in context_lower:
            return num * 1e4
        return num

    def _parse_percent(self, percent_str: str) -> Optional[float]:
        try:
            return float(percent_str) / 100.0
        except Exception:
            return None

    def _truncate_for_log(self, text: Any, limit: int = 1200) -> str:
        """将文本或结构化数据转换为可安全截断的字符串"""

        if text is None:
            return "None"

        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, default=str)

        if len(text) <= limit:
            return text

        return f"{text[:limit]}... (truncated, total={len(text)})"

    def _log_step_output(self, step: str, payload: Any, max_len: int = 1200) -> None:
        """统一输出财报分析中间结果，便于排查不透明问题"""

        serialized = payload
        if not isinstance(payload, str):
            serialized = json.dumps(payload, ensure_ascii=False, default=str)

        logger.debug(
            "财报分析中间结果 | step=%s | payload=%s",
            step,
            self._truncate_for_log(serialized, max_len),
        )

    def _extract_financial_data(
        self, stock_code: str, report_text: str, report_type: str, period_date: str
    ) -> Dict[str, Any]:
        """
        从财报文本中提取关键财务数据表格
        使用LLM识别并结构化财务数据
        """
        # 构建提示词（保留更长正文，避免丢失报表数字）
        max_chars = 128000
        truncated_text = report_text[:max_chars]

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

财报文本内容（已截取前 {max_chars} 字符，原文长度 {len(report_text)}）：
{truncated_text}
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
            logger.debug(
                "单轮财报提取LLM返回: stock=%s | snippet=%s",
                stock_code,
                self._truncate_for_log(llm_response, 500),
            )

            # 尝试解析JSON响应
            try:
                extracted_data = json.loads(llm_response)
                if isinstance(extracted_data, dict):
                    extracted_data["llm_response_raw"] = llm_response[:1000]
                    self._log_step_output(
                        "single_pass_extraction_parsed", extracted_data
                    )
                    return extracted_data
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
                        self._log_step_output(
                            "single_pass_extraction_parsed", extracted_data
                        )
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

    def _extract_financial_data_multipass(
        self, stock_code: str, report_text: str, report_type: str, period_date: str
    ) -> Dict[str, Any]:
        """
        多轮提取：每轮聚焦不同报表，但输入使用全文（截取上限）
        """

        max_chars = 128000
        truncated_text = report_text[:max_chars]

        passes = [
            (
                "income_balance",
                "仅提取利润表与资产负债表字段：收入/成本/费用/利润/总资产/总负债/权益/货币资金/应收/存货/流动与非流动拆分，严格JSON",
            ),
            (
                "cash_flow",
                "仅提取现金流量表关键字段：经营/投资/筹资现金流净额，严格JSON",
            ),
        ]

        merged_financial_data: Dict[str, Any] = {}
        llm_raw_snippets: List[str] = []
        errors: List[Dict[str, Any]] = []

        for name, focus in passes:
            prompt = f"""你是一名专业的财务分析师，请从以下上市公司财务报告中提取关键财务数据。

股票代码: {stock_code}
报告类型: {report_type}
报告期间: {period_date}

提取范围（本轮聚焦）：{focus}

返回JSON格式：
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

财报全文（已截取前 {max_chars} 字符，原文长度 {len(report_text)}）：
{truncated_text}
"""

            messages = [
                {
                    "role": "system",
                    "content": "你是一名专业的财务分析师，擅长从财务报告中提取结构化数据。请严格按JSON格式返回结果。",
                },
                {"role": "user", "content": prompt},
            ]

            response = self._call_chat_completion(
                messages, temperature=0.3, max_tokens=5000
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            logger.debug(
                "多轮提取LLM返回: stock=%s | pass=%s | snippet=%s",
                stock_code,
                name,
                self._truncate_for_log(llm_response, 600),
            )

            parsed: Dict[str, Any] = {
                "success": False,
                "error": "无法解析LLM返回的JSON",
            }

            json_match = None
            try:
                parsed = json.loads(llm_response)
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        parsed = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        pass

            if isinstance(parsed, dict):
                parsed["llm_response_raw"] = llm_response[:1000]
            else:
                parsed = {
                    "success": False,
                    "error": "LLM返回非JSON格式",
                    "llm_response": llm_response[:1000],
                }

            if not parsed.get("success"):
                errors.append({"pass": name, "error": parsed.get("error", "解析失败")})
                llm_raw_snippets.append(llm_response[:300])
                self._log_step_output(f"multipass_extraction_failure_{name}", parsed)
                continue

            llm_raw_snippets.append(llm_response[:300])
            fd = parsed.get("financial_data") or {}
            merged_financial_data = self._merge_financial_data(
                merged_financial_data, fd
            )
            self._log_step_output(f"multipass_extraction_parsed_{name}", parsed)

        if not merged_financial_data:
            self._log_step_output(
                "multipass_extraction_result_empty",
                {"errors": errors, "llm_snippets": llm_raw_snippets},
            )
            return {
                "success": False,
                "error": "未能提取到财务数据",
                "errors": errors,
                "llm_response_raw": " | ".join(llm_raw_snippets)[:1000],
            }

        self._log_step_output(
            "multipass_extraction_merged",
            {
                "financial_data": merged_financial_data,
                "errors": errors,
                "llm_snippets": llm_raw_snippets,
            },
        )
        return {
            "success": True,
            "financial_data": merged_financial_data,
            "extraction_confidence": "mixed",
            "notes": "multi-pass extraction",
            "llm_response_raw": " | ".join(llm_raw_snippets)[:1000],
        }

    def _merge_financial_data(
        self, base: Dict[str, Any], new: Dict[str, Any]
    ) -> Dict[str, Any]:
        """合并多次提取的财务数据，优先保留已有非空值"""

        def _merge(a: Any, b: Any) -> Any:
            if isinstance(a, dict) and isinstance(b, dict):
                merged = dict(a)
                for k, v in b.items():
                    if k in merged:
                        merged[k] = _merge(merged[k], v)
                    else:
                        merged[k] = v
                return merged
            if a in (None, "", []):
                return b
            return a

        return _merge(base or {}, new or {})

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
            logger.debug(
                "成本结构分析LLM返回: snippet=%s",
                self._truncate_for_log(llm_response, 800),
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                self._log_step_output("cost_structure_analysis_parsed", analysis_result)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        analysis_result = json.loads(json_match.group())
                        self._log_step_output(
                            "cost_structure_analysis_parsed", analysis_result
                        )
                        return analysis_result
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
            logger.debug(
                "利润分析LLM返回: snippet=%s",
                self._truncate_for_log(llm_response, 800),
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                self._log_step_output("profit_analysis_parsed", analysis_result)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        analysis_result = json.loads(json_match.group())
                        self._log_step_output("profit_analysis_parsed", analysis_result)
                        return analysis_result
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
        以DCF为主的估值（替代清算价值），聚焦内在价值和安全边际
        """

        prompt = f"""你是一名价值投资分析师，请基于DCF框架估算公司内在价值（万元口径），并给出安全边际。

输入财务数据（可能不全）：
{json.dumps(financial_data.get("financial_data", {}), indent=2, ensure_ascii=False)}

要求：
1) 使用简化FCFF模型，给出主要假设（收入增速、EBIT或利润率、折旧/资本开支、营运资金变化）
2) 估算WACC，给出取值理由；如缺数据，用行业保守值并说明
3) 给出核心结果：企业价值EV、股权价值、每股内在价值（如缺股本，用“未提供股本，无法算每股”提示）
4) 安全边际：基于当前股价/总市值（若缺，给出“缺市值，无法计算安全边际”）
5) 总结与结论：一句话结论（买入/中性/回避）

返回JSON：
{{
  "success": true/false,
  "liquidation_analysis": {{
    "method": "DCF",
    "assumptions": {{"revenue_growth": "", "margin": "", "capex": "", "wacc": ""}},
    "intrinsic_value": "企业价值(万元)",
    "equity_value": "股权价值(万元)",
    "fair_value_per_share": "每股内在价值(元)或无法计算",
    "safety_margin": "高/中/低/未知",
    "summary": "一句话结论，含买入/中性/回避"
  }}
}}
"""

        messages = [
            {
                "role": "system",
                "content": "你是一名价值投资分析师，擅长用DCF估算内在价值并给出安全边际。务必输出可解析的JSON。",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self._call_chat_completion(
                messages, temperature=0.3, max_tokens=3200
            )
            llm_response = (
                response.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            logger.debug(
                "DCF/估值分析LLM返回: snippet=%s",
                self._truncate_for_log(llm_response, 800),
            )

            try:
                analysis_result = json.loads(llm_response)
                self._log_step_output("liquidation_value_parsed", analysis_result)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        analysis_result = json.loads(json_match.group())
                        self._log_step_output(
                            "liquidation_value_parsed", analysis_result
                        )
                        return analysis_result
                    except json.JSONDecodeError:
                        pass

                return {
                    "success": False,
                    "error": "无法解析分析结果",
                    "llm_response": llm_response[:1000],
                }

        except Exception as e:
            logger.error(f"DCF估值计算时出错: {e}")
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
            logger.debug(
                "审计风险分析LLM返回: snippet=%s",
                self._truncate_for_log(llm_response, 800),
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                self._log_step_output("audit_insights_parsed", analysis_result)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        analysis_result = json.loads(json_match.group())
                        self._log_step_output("audit_insights_parsed", analysis_result)
                        return analysis_result
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
            logger.debug(
                "综合评估LLM返回: snippet=%s",
                self._truncate_for_log(llm_response, 800),
            )

            # 解析JSON响应
            try:
                analysis_result = json.loads(llm_response)
                self._log_step_output("overall_assessment_parsed", analysis_result)
                return analysis_result
            except json.JSONDecodeError:
                json_match = re.search(r"\{.*\}", llm_response, re.DOTALL)
                if json_match:
                    try:
                        analysis_result = json.loads(json_match.group())
                        self._log_step_output(
                            "overall_assessment_parsed", analysis_result
                        )
                        return analysis_result
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
