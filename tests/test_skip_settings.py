"""标的 skip 设置测试：skip_search / skip_signals 过滤 + 飞书 CRUD。"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestSkipHelpers:
    def test_get_skip_search(self):
        from analysis.portfolio_strategy import get_skip_search
        assert get_skip_search({"skip_search": ["601985", "000958"]}) == {"601985", "000958"}
        assert get_skip_search({}) == set()
        assert get_skip_search({"skip_search": None}) == set()

    def test_get_skip_signals(self):
        from analysis.portfolio_strategy import get_skip_signals
        assert get_skip_signals({"skip_signals": ["508091"]}) == {"508091"}
        assert get_skip_signals({}) == set()


class TestSkipCommandParse:
    def test_skip_search(self):
        from interactive.command_parser import parse_command, SkipCommand
        c = parse_command("/skip search 601985")
        assert isinstance(c, SkipCommand)
        assert c.kind == "search" and c.codes == ["601985"] and c.remove is False

    def test_skip_signals_multi(self):
        from interactive.command_parser import parse_command
        c = parse_command("/skip signals 508091,000958")
        assert c.kind == "signals" and c.codes == ["508091", "000958"]

    def test_unskip(self):
        from interactive.command_parser import parse_command
        c = parse_command("/unskip search 601985")
        assert c.kind == "search" and c.remove is True

    def test_skip_bad_kind(self):
        from interactive.command_parser import parse_command, ErrorCommand
        assert isinstance(parse_command("/skip foo 123"), ErrorCommand)


class TestHandleSkip:
    def _cfg(self):
        return {"stocks": ["601985", "000958", "508091"],
                "skip_search": [], "skip_signals": []}

    def test_add_skip_search(self):
        from interactive.commands import handlers
        cfg = self._cfg()
        saved = {}
        with patch.object(handlers, "_load_config", return_value=cfg), \
             patch.object(handlers, "_save_config", lambda c: saved.update(c)):
            out = handlers.handle_skip("search", ["601985"])
        assert "601985" in saved["skip_search"]
        assert "关闭" in out

    def test_unskip_restores(self):
        from interactive.commands import handlers
        cfg = {"stocks": ["601985"], "skip_search": ["601985"], "skip_signals": []}
        saved = {}
        with patch.object(handlers, "_load_config", return_value=cfg), \
             patch.object(handlers, "_save_config", lambda c: saved.update(c)):
            out = handlers.handle_skip("search", ["601985"], remove=True)
        assert "601985" not in saved["skip_search"]
        assert "恢复" in out

    def test_skip_ignores_non_monitored(self):
        from interactive.commands import handlers
        cfg = self._cfg()
        with patch.object(handlers, "_load_config", return_value=cfg), \
             patch.object(handlers, "_save_config", lambda c: None):
            out = handlers.handle_skip("search", ["999999"])  # 不在 stocks
        assert "无变更" in out

    def test_list_shows_skip_status(self):
        from interactive.commands import handlers
        cfg = {"stocks": ["601985", "000958"],
               "skip_search": ["601985"], "skip_signals": ["000958"]}
        with patch.object(handlers, "_load_config", return_value=cfg):
            out = handlers.handle_list()
        assert "不搜参: 1" in out and "不显示信号: 1" in out
        assert "<s>🔍</s>" in out  # 601985 搜参关闭
