"""Compatibility entrypoints for reading retired research archives only."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .archive import read_archive


class FundamentalQuantService:
    def __init__(self, config: dict, *, today=None, **_unused):
        settings = config.get("fundamental_quant", {}) or {}
        self.root = Path(settings.get("root", "data/fundamental_quant"))
        self.today = today or datetime.now(timezone(timedelta(hours=8))).date()

    def _retired(self):
        return {
            "status": "blocked",
            "historical": True,
            "as_of": self.today.isoformat(),
            "errors": [{"code": "legacy_fundamental_workflow_retired"}],
            "paid_model_calls": 0,
        }

    def run(self):
        return self._retired()

    def notify_weekly(self):
        return self._retired()

    def status(self):
        return self.query()

    def query(self, code=None):
        result = read_archive(self.root)
        result["as_of"] = self.today.isoformat()
        result["as_of_meaning"] = "archive read date, not research freshness"
        if code:
            for collection in (
                "companies", "publications", "longitudinal_reports", "sessions"
            ):
                result[collection] = [
                    item for item in result[collection]
                    if str(code) in item.get("identifiers", [])
                ]
            if not any(result[key] for key in (
                "companies", "publications", "longitudinal_reports", "sessions"
            )):
                result["errors"].append({"code": "company_not_in_research_registry"})
        return result

    def report(self):
        """Return saved artifact pointers without regenerating any file."""
        return self.query()
