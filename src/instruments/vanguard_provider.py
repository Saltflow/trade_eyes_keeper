"""Official Vanguard fund profile and portfolio-holding adapter."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional

from .classifier import detect_market
from .models import (
    FundHolding,
    FundProfile,
    InstrumentType,
    MetricStatus,
    MetricValue,
)
from .providers import HttpProvider, ProviderPayload, _float


class VanguardFundProfileProvider(HttpProvider):
    """Read the same public JSON used by Vanguard's product page."""

    PROFILE_URL = (
        "https://investor.vanguard.com/investment-products/etfs/profile/{ticker}"
    )
    HOLDINGS_URL = (
        "https://investor.vanguard.com/vmf/api/{ticker}/"
        "portfolio-holding/stock.json"
    )

    def fetch(
        self,
        code: str,
        evaluation_date: date,
        instrument_type: InstrumentType,
    ) -> ProviderPayload:
        payload = ProviderPayload()
        ticker = str(code).upper().strip()
        if (
            detect_market(ticker) != "us"
            or instrument_type
            not in {InstrumentType.INDEX_ETF, InstrumentType.SECTOR_ETF}
        ):
            payload.attempts.append(
                {
                    "source": "vanguard_official",
                    "status": "not_applicable",
                }
            )
            return payload

        fund = self._fetch_profile(ticker, evaluation_date, payload)
        if fund is None:
            return payload
        self._fetch_holdings(ticker, evaluation_date, fund, payload)
        payload.fund = fund
        return payload

    def _fetch_profile(
        self,
        ticker: str,
        evaluation_date: date,
        payload: ProviderPayload,
    ) -> Optional[FundProfile]:
        try:
            response = self._get(
                self.PROFILE_URL.format(ticker=ticker.lower())
            )
            match = re.search(
                r'<script id="fundProfileData" '
                r'type="application/json">(.*?)</script>',
                response.text,
                re.DOTALL,
            )
            if not match:
                raise ValueError("fundProfileData not found")
            profile = json.loads(match.group(1)).get("fundProfile", {})
            if str(profile.get("ticker", "")).upper() != ticker:
                raise ValueError("official profile ticker mismatch")
            if (
                not profile.get("isInternalFund")
                or profile.get("isExternalFund")
            ):
                raise ValueError("ticker is not an internal Vanguard fund")

            long_name = profile.get("longName")
            short_name = profile.get("shortName")
            payload.metadata.update(
                {
                    "name": long_name,
                    "asset_class": profile.get("type"),
                }
            )
            fund = FundProfile(
                issuer="Vanguard",
                tracking_index=(
                    str(short_name).removesuffix(" ETF")
                    if short_name
                    else None
                ),
                asset_class=profile.get("type"),
                region_exposure="United States",
            )
            expense = _float(profile.get("expenseRatio"))
            expense_date = str(
                profile.get("expenseRatioAsOfDate") or ""
            )[:10]
            if expense is not None:
                fund.expense_ratio = MetricValue(
                    value=expense,
                    status=MetricStatus.OBSERVED,
                    as_of=(
                        date.fromisoformat(expense_date)
                        if expense_date
                        else evaluation_date
                    ),
                    source="vanguard_official_profile",
                    source_url=response.url,
                    confidence=1.0,
                )
            payload.attempts.append(
                {
                    "source": "vanguard_official_profile",
                    "status": "success",
                    "url": response.url,
                }
            )
            return fund
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "vanguard_official_profile",
                    "status": "unavailable",
                    "reason": str(exc),
                }
            )
            return None

    def _fetch_holdings(
        self,
        ticker: str,
        evaluation_date: date,
        fund: FundProfile,
        payload: ProviderPayload,
    ) -> None:
        try:
            response = self._get(self.HOLDINGS_URL.format(ticker=ticker))
            raw = json.loads(response.text)
            holdings_date = date.fromisoformat(str(raw["asOfDate"])[:10])
            if holdings_date > evaluation_date:
                raise ValueError("holdings date is after evaluation date")
            entities = (raw.get("fund") or {}).get("entity", [])
            fund.top_holdings = [
                FundHolding(
                    code=str(item.get("ticker", "")).strip(),
                    name=item.get("longName"),
                    market="us",
                    currency="USD",
                    weight=float(item["percentWeight"]) / 100.0,
                    as_of=holdings_date,
                    source="vanguard_official_holdings",
                )
                for item in entities
                if item.get("ticker")
                and _float(item.get("percentWeight")) is not None
            ][:10]
            if not fund.top_holdings:
                raise ValueError("official holdings response is empty")
            fund.holdings_as_of = holdings_date
            payload.attempts.append(
                {
                    "source": "vanguard_official_holdings",
                    "status": "success",
                    "url": response.url,
                    "as_of": holdings_date.isoformat(),
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "vanguard_official_holdings",
                    "status": "failed",
                    "reason": str(exc),
                }
            )
