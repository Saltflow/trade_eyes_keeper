"""
近两年监控股票回测框架 - SessionManager统一数据源版本
"""

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)


class BacktestFramework:
    """回测框架 - SessionManager统一数据源版本"""

    def __init__(self, config_path="config/config.yaml"):
        """
        初始化回测框架

        Args:
            config_path: 配置文件路径
        """
        self.config_path = Path(config_path)
        self.config = None
        self.stock_codes = []
        self._data_source = None
        self._init_logging()

    def _init_logging(self):
        logging.basicConfig(level=logging.INFO)

    def load_config(self):
        if not self.config_path.exists():
            return False

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f)

            if "stocks" in self.config:
                stocks = self.config["stocks"]
                # 使用所有监控股票进行回测
                self.stock_codes = stocks
                return True
            return False
        except Exception:
            return False

    @property
    def data_source(self):
        if self._data_source is None:
            try:
                from src.data_source import DataSource

                self._data_source = DataSource(self.config or {})
                logger.info("DataSource 初始化成功")
            except Exception as e:
                logger.error(f"DataSource 初始化失败: {e}")
                return None
        return self._data_source

    def fetch_historical_data(self, stock_code, days=730):
        """
        获取历史数据 - 使用DataSource统一入口（含缓存管理 + 复权验证）

        Args:
            stock_code: 股票代码
            days: 需要的历史天数

        Returns:
            DataFrame: 历史数据
        """
        if self.data_source is None:
            logger.error(f"DataSource未初始化，无法获取数据: {stock_code}")
            return None

        try:
            data = self.data_source.fetch_stock_data(stock_code, days)
            if data is not None and not data.empty:
                logger.info(
                    f"从DataSource获取数据成功: {stock_code} ({len(data)}条记录)"
                )
                return data
            else:
                logger.warning(f"DataSource返回空数据: {stock_code}")
                return None
        except Exception as e:
            logger.error(f"DataSource获取数据失败: {stock_code}, 错误: {e}")
            return None

    def _calculate_start_dates(self):
        today = datetime.now()
        return [
            today - timedelta(days=2 * 365),
            today - timedelta(days=365),
            today - timedelta(days=6 * 30),
            today - timedelta(days=2 * 30),
        ]

    def _find_nearest_trading_date(self, target_date, price_data):
        """查找最近的交易日"""
        if price_data is None or price_data.empty or "date" not in price_data.columns:
            return target_date

        try:
            import pandas as pd

            dates = pd.to_datetime(price_data["date"])
            past_dates = dates[dates <= pd.Timestamp(target_date)]
            if not past_dates.empty:
                return past_dates.max().to_pydatetime()
            return dates.min().to_pydatetime()
        except Exception:
            return target_date

    def _calculate_buy_shares(self, amount, price, commission_rate=0.002):
        """计算买入股数（100股整数倍，含成本）"""
        if price <= 0:
            return 0
        available = amount * (1 - commission_rate)
        shares = int(available / price / 100) * 100
        return max(shares, 0)

    def _calculate_buy_cost(self, shares, price, commission_rate=0.002):
        """计算买入总成本"""
        if shares <= 0 or price <= 0:
            return 0
        return shares * price * (1 + commission_rate)

    def _calculate_single_stock_return(self, stock_code, start_date, amount=10000):
        """计算单个股票从起始日到今天的收益"""
        try:
            days_needed = (datetime.now() - start_date).days + 30
            price_data = self.fetch_historical_data(stock_code, days_needed)
            if price_data is None or price_data.empty:
                return None
            start_date_trading = self._find_nearest_trading_date(start_date, price_data)
            end_date_trading = self._find_nearest_trading_date(
                datetime.now(), price_data
            )
            import pandas as pd

            price_data["date_dt"] = pd.to_datetime(price_data["date"])
            start_price_row = price_data[
                price_data["date_dt"] == pd.Timestamp(start_date_trading)
            ]
            end_price_row = price_data[
                price_data["date_dt"] == pd.Timestamp(end_date_trading)
            ]
            if start_price_row.empty or end_price_row.empty:
                return None

            start_price = start_price_row.iloc[0]["close"]
            end_price = end_price_row.iloc[0]["close"]

            if start_price <= 0 or end_price <= 0:
                return None

            shares = self._calculate_buy_shares(amount, start_price)
            if shares <= 0:
                return None

            buy_cost = self._calculate_buy_cost(shares, start_price)
            sell_value = shares * end_price * (1 - 0.002)  # 卖出佣金
            profit = sell_value - buy_cost
            profit_pct = (profit / buy_cost) * 100 if buy_cost > 0 else 0

            return {
                "stock_code": stock_code,
                "start_date": start_date_trading.strftime("%Y-%m-%d"),
                "end_date": end_date_trading.strftime("%Y-%m-%d"),
                "start_price": round(start_price, 2),
                "end_price": round(end_price, 2),
                "shares": shares,
                "buy_cost": round(buy_cost, 2),
                "sell_value": round(sell_value, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit_pct, 2),
            }
        except Exception as e:
            logger.warning(f"计算股票 {stock_code} 收益失败: {e}")
            return None

    def run_backtest(self, initial_amount=10000):
        """
        运行回测

        Args:
            initial_amount: 初始资金（每只股票）

        Returns:
            list: 回测结果列表（email_notifier期望的格式）
        """
        if not self.load_config():
            logger.error("加载配置失败")
            return []

        if not self.stock_codes:
            logger.warning("没有股票代码")
            return []

        logger.info(
            f"开始回测，共{len(self.stock_codes)}只股票，初始资金{initial_amount}元"
        )

        start_dates = self._calculate_start_dates()
        # 按股票分组的结果
        stock_results_dict = {}

        for stock_code in self.stock_codes:
            stock_returns = []
            for start_date in start_dates:
                result = self._calculate_single_stock_return(
                    stock_code, start_date, initial_amount
                )
                if result:
                    # 转换为email_notifier期望的格式
                    stock_returns.append(
                        {
                            "current_value": result.get("sell_value"),
                            "profit_pct": result.get("profit_pct"),
                        }
                    )
                else:
                    stock_returns.append({"error": True})

            stock_results_dict[stock_code] = stock_returns

        # 转换为email_notifier期望的最终格式
        results = []
        for stock_code, returns in stock_results_dict.items():
            results.append(
                {
                    "stock_code": stock_code,
                    "returns": returns,
                }
            )

        logger.info(f"回测完成，共{len(results)}只股票的结果")
        return results

    def get_backtest_results(self, initial_amount=10000):
        """获取回测结果（向后兼容的别名）"""
        return self.run_backtest(initial_amount)

    def generate_report(self, results):
        """生成回测报告"""
        if not results:
            return "没有回测结果"

        import pandas as pd

        df = pd.DataFrame(results)

        report = []
        report.append("=" * 80)
        report.append("股票回测报告")
        report.append("=" * 80)
        report.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"回测股票数: {len(self.stock_codes)}")
        report.append(f"回测结果数: {len(results)}")
        report.append("")

        # 按时间段分组统计
        for period_days in sorted(df["period_days"].unique()):
            period_df = df[df["period_days"] == period_days]
            avg_profit = period_df["profit_pct"].mean()
            report.append(f"--- {period_days}天周期 ---")
            report.append(f"平均收益率: {avg_profit:.2f}%")
            report.append(f"样本数: {len(period_df)}")
            report.append("")

        report.append("=" * 80)
        report.append("详细结果:")
        report.append(df.to_string(index=False))
        report.append("=" * 80)

        return "\n".join(report)


if __name__ == "__main__":
    framework = BacktestFramework()
    results = framework.run_backtest()
    print(framework.generate_report(results))
