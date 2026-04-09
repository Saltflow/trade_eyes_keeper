"""
近两年监控股票回测框架 - 步骤2：WebCrawler集成
"""

import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)


class BacktestFramework:
    """回测框架 - WebCrawler集成版本"""

    def __init__(self, config_path="config/config.yaml"):
        self.config_path = Path(config_path)
        self.config = None
        self.stock_codes = []
        self.web_crawler = None
        self.data_cache = {}
        self.random_seed = random.randint(1, 1000)
        random.seed(self.random_seed)
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

    def _init_web_crawler(self):
        try:
            from src.web_crawler import StockWebCrawler

            self.web_crawler = StockWebCrawler(self.config or {})
            return True
        except Exception:
            return False

    def fetch_historical_data(self, stock_code, days=730):
        if stock_code in self.data_cache:
            return self.data_cache[stock_code]

        if not self.web_crawler and not self._init_web_crawler():
            return None

        # 确保web_crawler不为None
        if self.web_crawler is None:
            return None

        try:
            data = self.web_crawler.fetch_stock_data(stock_code, days)
            if data is not None and not data.empty:
                self.data_cache[stock_code] = data
                return data
            return None
        except Exception:
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
            if shares == 0:
                return None
            buy_cost = self._calculate_buy_cost(shares, start_price)
            current_value = shares * end_price
            profit = current_value - buy_cost
            profit_pct = profit / buy_cost if buy_cost > 0 else 0
            return {
                "stock_code": stock_code,
                "start_date": start_date_trading,
                "end_date": end_date_trading,
                "start_price": round(start_price, 4),
                "end_price": round(end_price, 4),
                "shares": shares,
                "buy_cost": round(buy_cost, 2),
                "current_value": round(current_value, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit_pct * 100, 2),
                "amount": amount,
            }
        except Exception:
            return None

    def _calculate_all_returns(self, amount=10000):
        """计算所有股票四个起始点的收益"""
        results = []
        dates = self._calculate_start_dates()
        date_labels = ["2年前", "1年前", "6个月前", "2个月前"]

        for stock_code in self.stock_codes:
            stock_results = {"stock_code": stock_code, "returns": []}
            for label, start_date in zip(date_labels, dates):
                result = self._calculate_single_stock_return(
                    stock_code, start_date, amount
                )
                if result:
                    stock_results["returns"].append(
                        {
                            "period": label,
                            "start_date": result["start_date"],
                            "end_date": result["end_date"],
                            "start_price": result["start_price"],
                            "end_price": result["end_price"],
                            "shares": result["shares"],
                            "buy_cost": result["buy_cost"],
                            "current_value": result["current_value"],
                            "profit": result["profit"],
                            "profit_pct": result["profit_pct"],
                        }
                    )
                else:
                    stock_results["returns"].append(
                        {"period": label, "error": "计算失败"}
                    )
            results.append(stock_results)
        return results

    def get_backtest_results(self):
        """获取回测结果数据（公共接口，不包含邮件发送）"""
        if not self.load_config():
            return None

        if not self._init_web_crawler():
            return None

        # 精简测试（可选，保持与run_backtest兼容）
        if self.stock_codes:
            test_stock = random.choice(self.stock_codes)
            if self.fetch_historical_data(test_stock, days=10) is not None:
                print(f"✅ 测试通过: {test_stock}")

        # 计算所有股票收益
        print("计算所有股票四个起始点收益...")
        results = self._calculate_all_returns(amount=10000)

        return results

    def _generate_html_report(self, results):
        """生成HTML报告表格"""
        # 生成表格部分
        table_html = '<table style="border-collapse: collapse; width: 100%; margin-top: 20px;">\n'
        table_html += "    <thead>\n"
        table_html += "        <tr>\n"
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">股票代码</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">2年前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">1年前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">6个月前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">2个月前持有至今</th>\n'
        table_html += "        </tr>\n"
        table_html += "    </thead>\n"
        table_html += "    <tbody>\n"

        for stock_result in results:
            table_html += f"        <tr>\n"
            table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center;">{stock_result["stock_code"]}</td>\n'
            for ret in stock_result["returns"]:
                if "error" in ret:
                    table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center; color: red;">计算失败</td>\n'
                else:
                    value = ret["current_value"]
                    profit_pct = ret["profit_pct"]
                    color = "green" if profit_pct >= 0 else "red"
                    table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center; color: {color};">{value:.2f}元<br><small>({profit_pct:+.2f}%)</small></td>\n'
            table_html += "        </tr>\n"

        table_html += "    </tbody>\n"
        table_html += "</table>\n"

        # 生成完整的HTML内容（包含标题和说明）
        html = f"""
<div style="font-family: Arial, sans-serif; margin: 20px;">
    <div style="text-align: center; margin-bottom: 30px;">
        <h1 style="color: #333; border-bottom: 1px solid #ddd; padding-bottom: 10px;">近两年监控股票回测报告</h1>
        <p style="color: #666; font-size: 14px;">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p>假设每个起始点买入1万元，计算到今天的价值（交易成本：买入千分之2）</p>
    </div>
    
    {table_html}
    
    <div style="margin-top: 30px; font-size: 12px; color: #888;">
        <p>注：</p>
        <ul>
            <li>数据来源：新浪财经/腾讯财经/东方财富公开数据</li>
            <li>交易成本：买入时收取千分之2（0.2%）手续费，卖出无费用</li>
            <li>最小交易单位：100股（手）</li>
            <li>不考虑分红和分红再投资</li>
            <li>日期处理：自动匹配最近的交易日</li>
        </ul>
    </div>
</div>
"""
        return html

    def run_backtest(self):
        # 获取回测结果（调用公共接口）
        results = self.get_backtest_results()
        if results is None:
            return False

        # 打印摘要
        for stock_result in results:
            print(f"\n股票 {stock_result['stock_code']}:")
            for ret in stock_result["returns"]:
                if "error" in ret:
                    print(f"  {ret['period']}: {ret['error']}")
                else:
                    print(
                        f"  {ret['period']}: 买入{ret['shares']}股，当前价值{ret['current_value']:.2f}元 (收益{ret['profit_pct']:.2f}%)"
                    )

        # 生成HTML报告
        print("\n生成HTML报告...")
        html_content = self._generate_html_report(results)
        report_path = "backtest_report.html"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"HTML报告已保存到: {report_path}")

        return True


if __name__ == "__main__":
    framework = BacktestFramework()
    success = framework.run_backtest()

    if success:
        print("\n✅ 回测框架步骤8（邮件发送集成）运行成功!")
    else:
        print("\n❌ 回测框架步骤8运行失败")

    exit(0 if success else 1)
