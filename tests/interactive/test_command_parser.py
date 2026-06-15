"""命令解析器测试 — TDD 第一步：先写会红的测试。"""

from src.interactive.command_parser import (
    AddCommand,
    BacktestCommand,
    CommandType,
    HelpCommand,
    ListCommand,
    RemoveCommand,
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
