"""命令解析器测试 — TDD 第一步：先写会红的测试。"""

from src.interactive.command_parser import (
    AddCommand,
    BacktestCommand,
    CommandType,
    HelpCommand,
    ListCommand,
    RemoveCommand,
    SaveCommand,
    parse_command,
)


class TestCommandParser:
    def test_parse_help(self):
        for text in ("/help", "/Help", "/HELP", "/help extra ignored"):
            cmd = parse_command(text)
            assert isinstance(cmd, HelpCommand)
            assert cmd.cmd_type == CommandType.HELP

    def test_parse_list(self):
        cmd = parse_command("/list")
        assert isinstance(cmd, ListCommand)
        assert cmd.cmd_type == CommandType.LIST

    def test_parse_add_valid(self):
        cmd = parse_command("/add 601728")
        assert isinstance(cmd, AddCommand)
        assert cmd.stock_code == "601728"

    def test_parse_add_missing_code(self):
        cmd = parse_command("/add")
        assert cmd.cmd_type == CommandType.ERROR
        assert "缺少股票代码" in cmd.message

    def test_parse_add_invalid_code(self):
        cmd = parse_command("/add not_a_stock!")
        assert cmd.cmd_type == CommandType.ERROR
        assert "格式无效" in cmd.message

    def test_parse_remove_valid(self):
        cmd = parse_command("/remove 00883")
        assert isinstance(cmd, RemoveCommand)
        assert cmd.stock_code == "00883"

    def test_parse_remove_missing_code(self):
        cmd = parse_command("/remove")
        assert cmd.cmd_type == CommandType.ERROR

    def test_parse_backtest_valid(self):
        cmd = parse_command("/backtest 601919 2024-01-01 2024-12-31")
        assert isinstance(cmd, BacktestCommand)
        assert cmd.stock_code == "601919"
        assert cmd.start_date == "2024-01-01"
        assert cmd.end_date == "2024-12-31"

    def test_parse_backtest_missing_dates(self):
        cmd = parse_command("/backtest 601919")
        assert cmd.cmd_type == CommandType.ERROR

    def test_parse_backtest_bad_date(self):
        cmd = parse_command("/backtest 601919 2024-13-01 2024-12-31")
        assert cmd.cmd_type == CommandType.ERROR
        assert "日期" in cmd.message

    def test_parse_unknown_command(self):
        cmd = parse_command("/unknown")
        assert cmd.cmd_type == CommandType.ERROR
        assert "未知" in cmd.message

    def test_parse_empty_message(self):
        cmd = parse_command("")
        assert cmd.cmd_type == CommandType.ERROR

    def test_parse_non_command(self):
        cmd = parse_command("hello world")
        assert cmd.cmd_type == CommandType.ERROR

    # ── 批量 add/remove ──

    def test_parse_add_comma_separated(self):
        cmd = parse_command("/add 601728,GOOG,00883")
        assert isinstance(cmd, AddCommand)
        assert cmd.codes == ["601728", "GOOG", "00883"]

    def test_parse_add_space_separated(self):
        cmd = parse_command("/add 601728 GOOG 00883")
        assert isinstance(cmd, AddCommand)
        assert cmd.codes == ["601728", "GOOG", "00883"]

    def test_parse_add_mixed_separator(self):
        cmd = parse_command("/add 601728, GOOG 00883")
        assert isinstance(cmd, AddCommand)
        assert cmd.codes == ["601728", "GOOG", "00883"]

    def test_parse_add_dedup(self):
        cmd = parse_command("/add 601728,601728, GOOG")
        assert isinstance(cmd, AddCommand)
        assert cmd.codes == ["601728", "GOOG"]

    def test_parse_add_stock_code_backcompat(self):
        cmd = parse_command("/add 601728")
        assert cmd.stock_code == "601728"
        assert cmd.codes == ["601728"]

    def test_parse_remove_batch(self):
        cmd = parse_command("/remove 00883,GOOG")
        assert isinstance(cmd, RemoveCommand)
        assert cmd.codes == ["00883", "GOOG"]

    def test_parse_save(self):
        cmd = parse_command("/save")
        assert isinstance(cmd, SaveCommand)

    # ── 真实代码格式覆盖 ──

    def test_valid_codes_from_config(self):
        """config.yaml 里出现的所有代码格式都应通过验证。"""
        real_codes = [
            "601728", "600938", "601985", "601919",  # 6-digit A-share
            "512810", "513910", "588000", "000958",  # ETF
            "515180", "508077", "180603",            # 混合
            "GOOG", "VOO", "TQQQ", "UPRO",           # US tickers
            "00883", "01816", "1355",                 # HK
            "C38U.SI", "AJBU.SI",                     # Singapore
        ]
        for code in real_codes:
            cmd = parse_command(f"/add {code}")
            assert isinstance(cmd, AddCommand), f"code={code} should be valid"
            assert code in cmd.codes, f"code={code} should be in codes"

    def test_valid_batch_with_real_codes(self):
        cmd = parse_command("/add C38U.SI,AJBU.SI, 513000,588510,518660")
        assert isinstance(cmd, AddCommand)
        assert "C38U.SI" in cmd.codes
        assert "AJBU.SI" in cmd.codes
        assert "513000" in cmd.codes
        assert "588510" in cmd.codes
        assert "518660" in cmd.codes

    def test_invalid_code_with_special_chars(self):
        cmd = parse_command("/add not_a_stock!")
        assert cmd.cmd_type == CommandType.ERROR
