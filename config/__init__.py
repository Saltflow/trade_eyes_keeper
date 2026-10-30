from pathlib import Path

import yaml


DEFAULT_ALERTS_CONFIG_PATH = Path(__file__).resolve().with_name("alerts.yaml")


class AlertsConfig:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_ALERTS_CONFIG_PATH
        with self.path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"Alert configuration must be a mapping: {self.path}")

        self.anchors = data.get(
            "anchors", [{"name": "ma60", "type": "daily_ma", "window": 60}]
        )
        self.thresholds = [-10, -5, 0, 5, 10, 15]
        self.boundary = {
            "neg_right_closed": True,
            "pos_left_closed": True,
            "exclude_zero": True,
            "skip_zero_five": True,
        }
        self.thresholds = data.get("thresholds", self.thresholds)
        self.boundary = {**self.boundary, **data.get("boundary_rules", {})}
        self.consecutive_days_threshold = data.get("consecutive_days_threshold", 5)
        self.auto_reset = data.get("auto_reset", True)
        self._validate()

    def to_dict(self):
        """Expose the same resolved rules to indicator and alert consumers."""
        return {
            "anchors": [dict(anchor) for anchor in self.anchors],
            "thresholds": list(self.thresholds),
            "boundary_rules": dict(self.boundary),
            "consecutive_days_threshold": self.consecutive_days_threshold,
            "auto_reset": self.auto_reset,
        }

    def _validate(self):
        if len(self.thresholds) < 2:
            raise ValueError("Need at least 2 thresholds")
        for i in range(1, len(self.thresholds)):
            if self.thresholds[i] <= self.thresholds[i - 1]:
                raise ValueError("Thresholds not increasing")
        if self.boundary.get("skip_zero_five", False):
            if 0 not in self.thresholds or 5 not in self.thresholds:
                raise ValueError("Skip 0-5 requires thresholds 0 and 5")

    def get_anchor(self, name):
        for a in self.anchors:
            if a.get("name") == name:
                return a
        return None

    def get_intervals(self):
        intervals = []
        for i in range(len(self.thresholds) - 1):
            lo, hi = self.thresholds[i], self.thresholds[i + 1]
            inc_lo, inc_hi = False, False
            if lo < 0 and self.boundary.get("neg_right_closed", True):
                inc_hi = True
            elif hi > 0 and self.boundary.get("pos_left_closed", True):
                inc_lo = True
            if lo < 0 and self.boundary.get("exclude_zero", True) and hi == 0:
                inc_hi = False
            interval = {
                "lower": lo,
                "upper": hi,
                "lower_inclusive": inc_lo,
                "upper_inclusive": inc_hi,
            }
            if self.boundary.get("skip_zero_five", True) and lo == 0 and hi == 5:
                interval["skip"] = True
            intervals.append(interval)
        return intervals


def get_alerts_config(path=None):
    """Read one caller-owned rules snapshot; paths and reloads never share state."""
    return AlertsConfig(path)
