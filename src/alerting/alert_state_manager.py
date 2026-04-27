"""
警报状态跟踪管理器
实现5天连续警报限制和状态持久化
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class AlertStateManager:
    """警报状态管理器"""

    def __init__(self, alerts_config, cache_dir=None):
        self.max_consecutive_days = (
            alerts_config.consecutive_days_threshold
            if hasattr(alerts_config, "consecutive_days_threshold")
            else 5
        )
        self.auto_reset = (
            alerts_config.auto_reset if hasattr(alerts_config, "auto_reset") else True
        )
        cache_path = Path(cache_dir) if cache_dir else Path("./cache/alerts")
        cache_path.mkdir(parents=True, exist_ok=True)
        self.state_file = cache_path / "alerts_state.json"
        self._state = self._load()

    def _load(self) -> Dict:
        if not self.state_file.exists():
            return {"last_updated": None, "alerts": {}}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"last_updated": None, "alerts": {}}

    def _save(self):
        self._state["last_updated"] = datetime.now().isoformat()
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def _key(self, stock_code: str, anchor_name: str, interval_label: str) -> str:
        return f"{stock_code}_{anchor_name}_{interval_label}"

    def should_alert(
        self,
        stock_code: str,
        anchor_name: str,
        interval_label: str,
        current_date: Optional[str] = None,
    ) -> Tuple[bool, int]:
        if current_date is None:
            current_date = datetime.now().date().isoformat()

        key = self._key(stock_code, anchor_name, interval_label)
        alerts = self._state.get("alerts", {})

        if key not in alerts:
            return True, 1

        entry = alerts[key]
        last_date = entry.get("last_date")
        consecutive = entry.get("consecutive_days", 0)

        if last_date != current_date:
            consecutive += 1

        return consecutive <= self.max_consecutive_days, consecutive

    def update(
        self,
        stock_code: str,
        anchor_name: str,
        interval_label: str,
        current_date: Optional[str] = None,
    ):
        if current_date is None:
            current_date = datetime.now().date().isoformat()

        key = self._key(stock_code, anchor_name, interval_label)
        alerts = self._state.setdefault("alerts", {})

        if key not in alerts:
            alerts[key] = {
                "stock_code": stock_code,
                "anchor_name": anchor_name,
                "interval_label": interval_label,
                "last_date": current_date,
                "consecutive_days": 1,
            }
        else:
            entry = alerts[key]
            if entry.get("last_date") != current_date:
                entry["consecutive_days"] = entry.get("consecutive_days", 0) + 1
                entry["last_date"] = current_date

        self._save()

    def reset_for_new_interval(
        self, stock_code: str, anchor_name: str, new_interval_label: str
    ):
        if not self.auto_reset:
            logger.debug(f"自动重置已禁用，跳过 {stock_code} {anchor_name}")
            return

        alerts = self._state.get("alerts", {})
        keys_to_remove = []

        for key in alerts:
            parts = key.split("_", 2)
            if len(parts) < 3:
                continue
            sc, an, _ = parts
            if sc == stock_code and an == anchor_name:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            parts = key.split("_", 2)
            if len(parts) < 3:
                continue
            _, _, old_interval = parts
            if old_interval != new_interval_label:
                del alerts[key]
                logger.debug(
                    f"重置: {stock_code} {anchor_name} 从 {old_interval} 切换到 {new_interval_label}"
                )

        if keys_to_remove:
            self._save()

    def clear_all(self):
        self._state = {"last_updated": None, "alerts": {}}
        self._save()
