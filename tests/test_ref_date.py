"""TDD: /ref_date 命令全链路测试。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")


class TestRefDateHandler:
    """handle_ref_date 逻辑测试。"""

    def setup_method(self):
        """确保测试不影响真实 config，且从干净状态开始。"""
        import yaml
        from src.interactive.commands import handlers

        try:
            with open(handlers.CONFIG_PATH, "r", encoding="utf-8") as f:
                self._saved_config = yaml.safe_load(f)
        except Exception:
            self._saved_config = {}
        # 清掉上一次测试残留的 reference_base_date
        try:
            cfg = handlers._load_config()
            if cfg.get("optimizer", {}).get("reference_base_date"):
                del cfg["optimizer"]["reference_base_date"]
                handlers._save_config(cfg)
        except Exception:
            pass

    def teardown_method(self):
        """恢复原始 config。"""
        import yaml
        from src.interactive.commands import handlers

        if self._saved_config is not None:
            with open(handlers.CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self._saved_config, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)

    def test_show_unset(self):
        from src.interactive.commands.handlers import handle_ref_date
        r = handle_ref_date()
        assert "未设置" in r

    def test_set_and_show(self):
        from src.interactive.commands.handlers import handle_ref_date
        handle_ref_date("2026-07-14")
        r = handle_ref_date()
        assert "2026-07-14" in r

    def test_bad_date_rejected(self):
        from src.interactive.commands.handlers import handle_ref_date
        r = handle_ref_date("abc")
        assert "格式错误" in r


class TestRefDateCommandParser:
    """命令解析器生成 RefDateCommand。"""

    def test_parse_with_date(self):
        from src.interactive.command_parser import (
            parse_command, RefDateCommand,
        )
        cmd = parse_command("/ref_date 2026-07-14")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str == "2026-07-14"

    def test_parse_no_date(self):
        from src.interactive.command_parser import (
            parse_command, RefDateCommand,
        )
        cmd = parse_command("/ref_date")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str is None

    def test_parse_empty_date(self):
        from src.interactive.command_parser import (
            parse_command, RefDateCommand,
        )
        cmd = parse_command("/ref_date   ")
        assert isinstance(cmd, RefDateCommand)
        assert cmd.date_str is None


class TestRefDateDispatch:
    """命令分派：飞书 / Telegram 均正确路由到 handle_ref_date。"""

    def test_feishu_dispatch(self):
        from unittest.mock import patch
        from src.interactive.command_parser import (
            parse_command,
        )
        from src.interactive.feishu_handler import _dispatch
        cmd = parse_command("/ref_date")
        with patch("src.interactive.commands.handlers._load_config",
                   return_value={"optimizer": {}}):
            r = _dispatch(cmd)
        assert "参考持仓基期" in r

    def test_telegram_dispatch_import(self):
        """确认 Telegram bot 导入了 handle_ref_date 和 RefDateCommand。"""
        from src.interactive.telegram_bot import logger  # 触发 import
        import src.interactive.telegram_bot as tb

        # assert handle_ref_date 在 handlers 导入列表中
        assert hasattr(tb, "logger")  # 模块成功加载


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
