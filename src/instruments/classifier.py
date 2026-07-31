"""Market and instrument-type classification without code-specific portfolios."""

from __future__ import annotations

from .models import InstrumentType


US_ETF_SYMBOLS = {
    "BND",
    "DIA",
    "EEM",
    "EFA",
    "IWM",
    "QQQ",
    "SPY",
    "VTI",
    "VOO",
    "XLF",
}


def detect_market(code: str) -> str:
    normalized = str(code).upper().strip()
    if normalized.isdigit() and len(normalized) <= 5:
        return "hk"
    if normalized.isdigit() and len(normalized) == 6:
        return "a_share"
    return "us"


def normalize_yahoo_symbol(code: str, market: str | None = None) -> str:
    normalized = str(code).upper().strip()
    market = market or detect_market(normalized)
    if market == "us":
        return normalized.replace(".", "-")
    if market == "hk":
        return f"{(normalized.lstrip('0') or '0').zfill(4)}.HK"
    suffix = "SS" if normalized.startswith(("5", "6", "9")) else "SZ"
    return f"{normalized}.{suffix}"


def classify_instrument(
    code: str,
    *,
    quote_type: str | None = None,
    name: str | None = None,
    configured_type: str | None = None,
) -> InstrumentType:
    if configured_type:
        try:
            return InstrumentType(configured_type)
        except ValueError:
            pass

    normalized = str(code).upper().strip()
    upper_name = (name or "").upper()

    # Public infrastructure REITs trade in the 508xxx range.  They are not
    # exchange-traded index funds even though the legacy ETF detector said so.
    if normalized.isdigit() and normalized.startswith("508"):
        return InstrumentType.REIT
    if "REIT" in upper_name or "房地产投资信托" in (name or ""):
        return InstrumentType.REIT

    is_fund = quote_type in {"ETF", "MUTUALFUND"} or normalized in US_ETF_SYMBOLS
    is_fund = is_fund or (
        normalized.isdigit()
        and len(normalized) == 6
        and normalized.startswith(
            ("15", "16", "18", "51", "52", "56", "58")
        )
    )
    if not is_fund:
        return InstrumentType.EQUITY

    if any(word in upper_name for word in ("GOLD", "SILVER", "COMMODITY")) or any(
        word in (name or "") for word in ("黄金", "白银", "商品")
    ):
        return InstrumentType.COMMODITY_ETF
    if any(word in upper_name for word in ("BOND", "TREASURY")) or any(
        word in (name or "") for word in ("债券", "国债", "信用债", "可转债")
    ):
        return InstrumentType.BOND_ETF
    if any(
        word in (name or "")
        for word in ("行业", "军工", "银行", "证券", "医药", "能源", "科技")
    ):
        return InstrumentType.SECTOR_ETF
    if quote_type == "MUTUALFUND" and "指数" not in (name or ""):
        return InstrumentType.ACTIVE_FUND
    return InstrumentType.INDEX_ETF


def applicable_metrics(instrument_type: InstrumentType) -> list[str]:
    identity = ["name", "market", "exchange", "currency", "instrument_type"]
    if instrument_type == InstrumentType.EQUITY:
        return identity + [
            "total_shares",
            "parent_equity",
            "revenue",
            "net_income_parent",
            "pe_ttm",
            "pb",
            "roe_ttm",
            "revenue_yoy",
            "revenue_qoq",
            "net_income_yoy",
            "net_income_qoq",
            "dividend_yield",
        ]
    if instrument_type == InstrumentType.REIT:
        return identity + [
            "nav_per_unit",
            "p_nav",
            "ffo_per_unit",
            "p_ffo",
            "distribution_yield",
            "occupancy_rate",
        ]
    base_fund = identity + [
        "tracking_index",
        "aum",
        "expense_ratio",
        "nav_per_unit",
        "premium_discount_rate",
        "dividend_yield",
    ]
    if instrument_type in {InstrumentType.INDEX_ETF, InstrumentType.SECTOR_ETF}:
        return base_fund + ["top_holdings", "look_through"]
    if instrument_type == InstrumentType.BOND_ETF:
        return base_fund + ["duration", "yield_to_maturity"]
    if instrument_type == InstrumentType.COMMODITY_ETF:
        return base_fund + ["tracking_difference"]
    return base_fund
