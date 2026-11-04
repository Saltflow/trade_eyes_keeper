from pathlib import Path

import yaml

DEFAULT_ALERTS_CONFIG_PATH = Path(__file__).resolve().with_name("alerts.yaml")


class AlertsConfig:
    """Indicator anchors stored under the established alerts.yaml path."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_ALERTS_CONFIG_PATH
        with self.path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"Alert configuration must be a mapping: {self.path}")

        self.anchors = data.get(
            "anchors", [{"name": "ma60", "type": "daily_ma", "window": 60}]
        )

    def to_dict(self):
        """Expose anchor definitions without the retired alert-state rules."""
        return {"anchors": [dict(anchor) for anchor in self.anchors]}


def get_alerts_config(path=None):
    """Read one caller-owned rules snapshot; paths and reloads never share state."""
    return AlertsConfig(path)
