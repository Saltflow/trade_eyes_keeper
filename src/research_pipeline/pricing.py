"""Read official published tariffs; never confuse reservations with invoices."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .common import atomic_write, file_sha, load, now

URL = "https://api-docs.deepseek.com/quick_start/pricing/"
CNY_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


def parse_prices(document: str, *, cny=False) -> dict:
    soup = BeautifulSoup(document, "html.parser")
    rates: dict[str, dict[str, str]] = {"off_peak": {}, "peak": {}}
    metric = None
    for row in soup.select("tr"):
        text = " ".join(row.stripped_strings)
        normalized = text.lower().replace(" ", "")
        if "cachehit" in normalized or "缓存命中" in normalized:
            metric = "cached_input"
        elif "cachemiss" in normalized or "缓存未命中" in normalized:
            metric = "input"
        elif "outputtokens" in normalized or "百万tokens输出" in normalized:
            metric = "output"
        values = re.findall(
            r"([0-9]+(?:\.[0-9]+)?)\s*元" if cny else r"\$\s*([0-9]+(?:\.[0-9]+)?)",
            text,
        )
        phase = (
            "off_peak"
            if "off-peak" in text.lower() or "空闲时段" in text
            else "peak"
            if "peak" in text.lower() or "高峰时段" in text
            else None
        )
        if metric and phase and values:
            rates[phase][metric] = values[0]
    if any(
        set(values) != {"input", "output", "cached_input"} for values in rates.values()
    ):
        raise ValueError("official_pricing_format_unrecognized")
    if any(
        not Decimal(value).is_finite() or Decimal(value) <= 0
        for values in rates.values()
        for value in values.values()
    ):
        raise ValueError("invalid_official_tariff")
    if "deepseek-flash" not in document:
        raise ValueError("flash_tariff_not_found")
    return {
        "model": "deepseek-flash",
        "rates": rates,
        "source": CNY_URL if cny else URL,
        "verified_at": now(),
        "verified": True,
    }


def tariff_phase(
    at: datetime | None = None, chinese_holidays: tuple[str, ...] | None = None
) -> str:
    at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    from zoneinfo import ZoneInfo

    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    if at.weekday() >= 5 or not (1 <= at.hour < 4 or 6 <= at.hour < 10):
        return "off_peak"
    day = local.date().isoformat()
    if chinese_holidays is not None:
        return "off_peak" if day in chinese_holidays else "peak"
    path = Path(__file__).resolve().parents[2] / "config/research_tariff_calendar.json"
    try:
        calendar = load(path)["years"][str(local.year)]
        if not calendar.get("source", "").startswith("https://www.beijing.gov.cn/"):
            raise ValueError("unverified_public_holiday_calendar")
        return (
            "off_peak"
            if any(start <= day <= end for start, end in calendar["holiday_ranges"])
            else "peak"
        )
    except (OSError, KeyError, ValueError, TypeError):
        raise ValueError("pricing_holiday_calendar_unverified_no_new_POST") from None


class PriceService:
    def __init__(self, root: Path, http=None):
        self.root = Path(root)
        self.http = http or requests.Session()

    def refresh(self) -> dict:
        response = self.http.get(URL, timeout=(10, 30), allow_redirects=False)
        response.raise_for_status()
        value = parse_prices(response.content.decode("utf-8"))
        cny_response = self.http.get(CNY_URL, timeout=(10, 30), allow_redirects=False)
        cny_response.raise_for_status()
        cny = parse_prices(cny_response.content.decode("utf-8"), cny=True)
        ratios = {
            Decimal(rate) / Decimal(cny["rates"][phase][metric])
            for phase, rates in value["rates"].items()
            for metric, rate in rates.items()
        }
        if len(ratios) != 1:
            raise ValueError("official_USD_CNY_tariff_ratios_disagree")
        value["account_tariff_ratio"] = str(ratios.pop())
        value["account_tariff_ratio_is_exchange_rate"] = False
        value["cny_rates"] = cny["rates"]
        raw = self.root / "pricing" / (value["verified_at"].replace(":", "_") + ".html")
        atomic_write(raw, response.content, exclusive=True)
        value["source_sha256"] = file_sha(raw)
        value["raw_path"] = str(raw)
        cny_raw = raw.with_name(raw.stem + "_CNY.html")
        atomic_write(cny_raw, cny_response.content, exclusive=True)
        value["cny_source"] = CNY_URL
        value["cny_raw_path"] = str(cny_raw)
        value["cny_source_sha256"] = file_sha(cny_raw)
        atomic_write(self.root / "pricing/latest.json", value)
        return value

    def cached(self) -> dict | None:
        try:
            value = load(self.root / "pricing/latest.json")
            if (
                value["verified"]
                and file_sha(Path(value["raw_path"])) == value["source_sha256"]
                and file_sha(Path(value["cny_raw_path"])) == value["cny_source_sha256"]
            ):
                return value
        except (OSError, ValueError, KeyError):
            pass
        return None
