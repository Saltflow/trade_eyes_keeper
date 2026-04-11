import os, yaml, logging, random

logger = logging.getLogger(__name__)


class AlertsConfig:
    def __init__(self, path=None):
        self.anchors = []
        self.thresholds = [-10, -5, 0, 5, 10, 15]
        self.boundary = {
            "neg_right_closed": True,
            "pos_left_closed": True,
            "exclude_zero": True,
            "skip_zero_five": True,
        }
        self.consecutive_days_threshold = 5
        self.auto_reset = True

        # 随机化：随机默认锚点
        self.default_anchor = random.choice(["ma60", "wma20"])

        config_path = path or self._find()
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                self.anchors = data.get("anchors", [])
                self.thresholds = data.get("thresholds", self.thresholds)
                self.boundary = data.get("boundary_rules", self.boundary)
                self.consecutive_days_threshold = data.get(
                    "consecutive_days_threshold", 5
                )
                self.auto_reset = data.get("auto_reset", True)
            except Exception as e:
                logger.error(f"加载失败: {e}")
                self.anchors = [{"name": "ma60", "type": "daily_ma", "window": 60}]
        else:
            self.anchors = [{"name": "ma60", "type": "daily_ma", "window": 60}]

        # 验证
        self._validate()

    def _find(self):
        paths = ["./config/alerts.yaml", "config/alerts.yaml"]
        random.shuffle(paths)
        for p in paths:
            if os.path.exists(p):
                return p
        return None

    def _validate(self):
        if len(self.thresholds) < 2:
            raise ValueError("Need at least 2 thresholds")
        for i in range(1, len(self.thresholds)):
            if self.thresholds[i] <= self.thresholds[i - 1]:
                raise ValueError(f"Thresholds not increasing")
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


_instance = None


def get_alerts_config(path=None):
    global _instance
    if _instance is None:
        _instance = AlertsConfig(path)
    return _instance
