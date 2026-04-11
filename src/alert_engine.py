"""
多层级警报规则引擎
根据配置的锚点和阈值计算价格区间
"""

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class AlertEngine:
    """警报规则引擎核心"""

    def __init__(self, alerts_config):
        self.config = alerts_config

    def _calc_pct(self, price, anchor):
        """计算百分比差: (price/anchor - 1) * 100"""
        if price is None or anchor is None or price <= 0 or anchor <= 0:
            return None
        try:
            return (price / anchor - 1) * 100
        except Exception:
            return None

    def _get_interval(self, pct):
        """根据百分比找到区间"""
        if pct is None:
            return None

        thresholds = self.config.thresholds
        intervals = self.config.get_intervals()

        # 检查是否在阈值范围外
        if pct < thresholds[0]:
            upper_incl = True if thresholds[0] < 0 else False
            return {
                "lower": -np.inf,
                "upper": thresholds[0],
                "lower_incl": False,
                "upper_incl": upper_incl,
                "label": f"<{thresholds[0]}%",
            }

        if pct >= thresholds[-1]:
            lower_incl = True if thresholds[-1] > 0 else False
            return {
                "lower": thresholds[-1],
                "upper": np.inf,
                "lower_incl": lower_incl,
                "upper_incl": False,
                "label": f">={thresholds[-1]}%",
            }

        # 在区间内查找
        for interval in intervals:
            if interval.get("skip", False):
                continue

            lo, hi = interval["lower"], interval["upper"]
            lo_inc = interval.get("lower_inclusive", False)
            hi_inc = interval.get("upper_inclusive", False)

            # 应用边界规则
            in_lower = pct > lo if not lo_inc else pct >= lo
            in_upper = pct < hi if not hi_inc else pct <= hi

            if in_lower and in_upper:
                # 生成标签
                lo_sym = "[" if lo_inc else "("
                hi_sym = "]" if hi_inc else ")"
                return {
                    "lower": lo,
                    "upper": hi,
                    "lower_incl": lo_inc,
                    "upper_incl": hi_inc,
                    "label": f"{lo_sym}{lo}%, {hi}%{hi_sym}",
                }

        return None

    def evaluate_anchor(self, stock_code, price, anchor_name, anchor_value):
        """评估单个锚点"""
        if anchor_value is None:
            return None

        pct = self._calc_pct(price, anchor_value)
        if pct is None:
            return None

        interval = self._get_interval(pct)
        if interval is None:
            return None

        return {
            "stock_code": stock_code,
            "anchor_name": anchor_name,
            "anchor_value": float(anchor_value),
            "price": float(price),
            "percentage": float(pct),
            "interval": interval,
        }

    def evaluate_stock(self, stock_data):
        """评估单只股票的所有锚点"""
        stock_code = stock_data.get("stock_code", "")
        low_price = stock_data.get("low")

        if not stock_code or pd.isna(low_price):
            return []

        results = []
        for anchor_cfg in self.config.anchors:
            anchor_name = anchor_cfg.get("name")
            if not anchor_name or anchor_name not in stock_data:
                continue

            anchor_val = stock_data[anchor_name]
            if pd.isna(anchor_val):
                continue

            result = self.evaluate_anchor(
                stock_code, low_price, anchor_name, anchor_val
            )
            if result:
                results.append(result)

        # 按百分比绝对值排序
        results.sort(key=lambda x: abs(x["percentage"]), reverse=True)
        return results
