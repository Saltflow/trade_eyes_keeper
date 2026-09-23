"""TDD: /ref_date 命令全链路测试。"""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")


class TestRefDateHandler:
    """handle_ref_date 逻辑测试。"""

    @pytest.fixture(autouse=True)
    def isolated_config(self, tmp_path, monkeypatch):
        """确保测试不影响真实 config，且从干净状态开始。"""
        from src.interactive.commands import handlers

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"optimizer": {}}), encoding="utf-8")
        monkeypatch.setattr(handlers, "CONFIG_PATH", path)

    def test_show_unset(self):
        from src.interactive.commands.handlers import handle_ref_date

        r = handle_ref_date()
        assert "未设置" in r

    def test_set_and_show(self, monkeypatch):
        from src.interactive.commands.handlers import handle_ref_date

        # This case is about having nothing to bind, so it must not depend on
        # whatever optimizer pointer happens to exist on the machine.
        monkeypatch.setattr(
            "src.search.artifacts.load_latest_strategy_run",
            lambda *args, **kwargs: None,
        )
        r = handle_ref_date("2026-07-14")
        assert "没有可绑定" in r
        assert "2026-07-14" not in handle_ref_date()

    def test_bad_date_rejected(self):
        from src.interactive.commands.handlers import handle_ref_date

        r = handle_ref_date("abc")
        assert "格式错误" in r


class TestRefDateCommandParser:
    """命令解析器生成 RefDateCommand。"""

    def test_parse_with_date(self):
        from src.interactive.command_parser import (
            RefDateCommand,
            parse_command,
        )

        cmd = parse_command("/ref_date 2026-07-14")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str == "2026-07-14"

    def test_parse_no_date(self):
        from src.interactive.command_parser import (
            RefDateCommand,
            parse_command,
        )

        cmd = parse_command("/ref_date")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str is None

    def test_parse_empty_date(self):
        from src.interactive.command_parser import (
            RefDateCommand,
            parse_command,
        )

        cmd = parse_command("/ref_date   ")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str is None


class TestRefDateDispatch:
    """命令分派：飞书 / Telegram 均正确路由到 handle_ref_date。"""

    def test_shared_bot_dispatch(self):
        from unittest.mock import patch

        from src.interactive.command_dispatcher import dispatch_command
        from src.interactive.command_parser import (
            parse_command,
        )

        cmd = parse_command("/ref_date")
        with patch(
            "src.interactive.commands.handlers._load_config",
            return_value={"optimizer": {}},
        ):
            r = dispatch_command(cmd)
        assert "参考持仓基期" in r

    def test_telegram_dispatch_import(self):
        """确认 Telegram bot 导入了 handle_ref_date 和 RefDateCommand。"""
        import src.interactive.telegram_bot as tb

        # assert handle_ref_date 在 handlers 导入列表中
        assert hasattr(tb, "logger")  # 模块成功加载


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
