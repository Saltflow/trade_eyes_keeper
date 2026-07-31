"""Pure, testable valuation, growth and fund look-through calculations."""

from __future__ import annotations

from datetime import date
from math import isfinite
from statistics import median
from typing import Iterable, Optional

from .models import (
    CompanyFundamentals,
    FinancialStatementSnapshot,
    FundHolding,
    FundProfile,
    GrowthMetric,
    LookThroughMetric,
    MetricStatus,
    MetricValue,
)


def _positive(value: Optional[float]) -> bool:
    return value is not None and isfinite(value) and value > 0


def available_statements(
    statements: Iterable[FinancialStatementSnapshot],
    evaluation_date: date,
) -> list[FinancialStatementSnapshot]:
    """Exclude unpublished statements; unknown publication dates are current-only."""
    result = []
    for statement in statements:
        if statement.period_end > evaluation_date:
            continue
        if statement.published_at and statement.published_at > evaluation_date:
            continue
        if statement.published_at is None and evaluation_date < date.today():
            # Unknown filing dates are safe only for a current snapshot audit.
            continue
        result.append(statement)
    return sorted(
        result, key=lambda item: (item.period_end, item.published_at or date.max)
    )


def to_standalone_quarters(
    statements: Iterable[FinancialStatementSnapshot],
) -> list[FinancialStatementSnapshot]:
    """Convert cumulative YTD statements to standalone quarters."""
    ordered = sorted(statements, key=lambda item: item.period_end)
    by_year: dict[int, list[FinancialStatementSnapshot]] = {}
    standalone: list[FinancialStatementSnapshot] = []
    for statement in ordered:
        by_year.setdefault(statement.period_end.year, []).append(statement)

    flow_fields = (
        "revenue",
        "net_income_parent",
        "adjusted_net_income_parent",
        "operating_cash_flow",
        "free_cash_flow",
    )
    for year_statements in by_year.values():
        previous_cumulative: Optional[FinancialStatementSnapshot] = None
        for statement in sorted(year_statements, key=lambda item: item.period_end):
            copy = statement.copy(deep=True)
            if statement.is_cumulative and previous_cumulative is not None:
                for field in flow_fields:
                    current = getattr(statement, field)
                    previous = getattr(previous_cumulative, field)
                    if current is not None and previous is not None:
                        setattr(copy, field, current - previous)
                copy.is_cumulative = False
                copy.period_type = "quarter"
                copy.diagnostics.append("derived_from_cumulative_statement")
            elif statement.is_cumulative:
                copy.is_cumulative = False
                copy.period_type = "quarter"
            standalone.append(copy)
            if statement.is_cumulative:
                previous_cumulative = statement
    return sorted(standalone, key=lambda item: item.period_end)


def _growth(
    current: Optional[float],
    prior: Optional[float],
    current_period: date,
    prior_period: date,
    source: str,
    epsilon: float = 1e-9,
) -> GrowthMetric:
    if current is None or prior is None:
        return GrowthMetric(
            current_value=current,
            prior_value=prior,
            current_period=current_period,
            prior_period=prior_period,
            source=source,
        )
    if abs(prior) <= epsilon:
        return GrowthMetric(
            status=MetricStatus.NOT_MEANINGFUL,
            interpretation="基期接近零",
            current_value=current,
            prior_value=prior,
            current_period=current_period,
            prior_period=prior_period,
            source=source,
        )
    if prior < 0 <= current:
        interpretation = "扭亏"
        status = MetricStatus.NOT_MEANINGFUL
        value = None
    elif prior >= 0 > current:
        interpretation = "转亏"
        status = MetricStatus.NOT_MEANINGFUL
        value = None
    else:
        interpretation = None
        status = MetricStatus.DERIVED
        value = (current / prior - 1.0) * 100.0
    return GrowthMetric(
        value_pct=value,
        status=status,
        interpretation=interpretation,
        current_value=current,
        prior_value=prior,
        current_period=current_period,
        prior_period=prior_period,
        source=source,
    )


def _sum_last(
    statements: list[FinancialStatementSnapshot],
    field: str,
    count: int = 4,
) -> Optional[float]:
    values = [getattr(item, field) for item in statements[-count:]]
    if len(values) != count or any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))

def _regular_quarters(
    statements: list[FinancialStatementSnapshot],
    count: int,
) -> list[FinancialStatementSnapshot]:
    """Return a quarterly suffix only when its date cadence is truly quarterly."""
    if len(statements) < count:
        return []
    selected = statements[-count:]
    gaps = [
        (current.period_end - previous.period_end).days
        for previous, current in zip(selected, selected[1:])
    ]
    if any(gap < 55 or gap > 125 for gap in gaps):
        return []
    return selected
def _latest_balance_statement(
    statements: list[FinancialStatementSnapshot],
) -> Optional[FinancialStatementSnapshot]:
    candidates = [
        statement
        for statement in statements
        if any(
            value is not None
            for value in (
                statement.parent_equity,
                statement.book_value_per_share,
                statement.common_shares_outstanding,
                statement.total_shares,
            )
        )
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda statement: (
            statement.period_end,
            sum(
                value is not None
                for value in (
                    statement.parent_equity,
                    statement.book_value_per_share,
                    statement.common_shares_outstanding,
                    statement.total_shares,
                )
            ),
            statement.period_type in {"year", "annual", "12M"},
        ),
    )


def _valuation_shares(
    balance: FinancialStatementSnapshot,
    earnings: Optional[FinancialStatementSnapshot],
) -> tuple[Optional[float], str]:
    """Choose the share basis compatible with the traded class and diluted EPS."""
    reported = balance.common_shares_outstanding or balance.total_shares
    reference = earnings or balance
    diluted = (
        balance.diluted_average_shares
        or reference.diluted_average_shares
    )
    income = reference.net_income_parent
    eps = reference.diluted_eps
    if (
        _positive(reported)
        and _positive(diluted)
        and income is not None
        and eps is not None
        and eps != 0
    ):
        scale = max(abs(eps), 1e-12)
        reported_error = abs(income / reported - eps) / scale
        diluted_error = abs(income / diluted - eps) / scale
        if diluted_error + 0.05 < reported_error:
            return (
                diluted,
                "稀释加权股数与报告 EPS/交易股类相容",
            )
    if _positive(reported):
        return reported, "期末普通股股数"
    if _positive(diluted):
        return diluted, "缺期末股本，使用稀释加权平均股数"
    return None, "无可用股本"





def derive_company_fundamentals(
    statements: Iterable[FinancialStatementSnapshot],
    *,
    current_price: MetricValue,
    evaluation_date: date,
    quoted_pe: MetricValue | None = None,
    quoted_pb: MetricValue | None = None,
    ttm_dividend_per_share: MetricValue | None = None,
    latest_dividend_per_share: MetricValue | None = None,
) -> CompanyFundamentals:
    available = available_statements(statements, evaluation_date)
    quarters = [
        item
        for item in to_standalone_quarters(available)
        if item.period_type in {"quarter", "3M"}
    ]
    annuals = [
        item
        for item in available
        if item.period_type in {"year", "annual", "12M"}
    ]
    latest_annual = annuals[-1] if annuals else None
    latest = _latest_balance_statement(available)
    result = CompanyFundamentals(
        statements=available,
        quoted_pe=quoted_pe or MetricValue(),
        quoted_pb=quoted_pb or MetricValue(),
        ttm_dividend_per_share=ttm_dividend_per_share or MetricValue(),
        latest_dividend_per_share=latest_dividend_per_share or MetricValue(),
    )
    if latest is None:
        return result

    shares, shares_note = _valuation_shares(latest, latest_annual)
    if _positive(shares):
        result.total_shares = MetricValue(
            value=shares,
            status=MetricStatus.OBSERVED,
            as_of=latest.period_end,
            published_at=latest.published_at,
            source=latest.source,
            note=shares_note,
        )
    book_value_per_share = latest.book_value_per_share
    if (
        book_value_per_share is None
        and _positive(shares)
        and latest.parent_equity is not None
    ):
        book_value_per_share = latest.parent_equity / shares
    if _positive(book_value_per_share):
        result.book_value_per_share = MetricValue(
            value=book_value_per_share,
            status=MetricStatus.DERIVED,
            as_of=latest.period_end,
            published_at=latest.published_at,
            source=latest.source,
            currency=latest.currency,
            note=f"归母净资产/估值股本（{shares_note}）",
        )

    ttm_quarters = _regular_quarters(quarters, 4)
    quarter_revenue = _sum_last(ttm_quarters, "revenue")
    quarter_income = _sum_last(ttm_quarters, "net_income_parent")
    complete_quarter_ttm = (
        bool(ttm_quarters)
        and quarter_revenue is not None
        and quarter_income is not None
    )
    if complete_quarter_ttm:
        ttm_revenue = quarter_revenue
        ttm_income = quarter_income
        ttm_adjusted = _sum_last(
            ttm_quarters,
            "adjusted_net_income_parent",
        )
        ttm_statement = ttm_quarters[-1]
    elif latest_annual is not None:
        ttm_revenue = latest_annual.revenue
        ttm_income = latest_annual.net_income_parent
        ttm_adjusted = latest_annual.adjusted_net_income_parent
        ttm_statement = latest_annual
    else:
        ttm_revenue = None
        ttm_income = None
        ttm_adjusted = None
        ttm_statement = None
    for attr, value in (
        ("ttm_revenue", ttm_revenue),
        ("ttm_net_income_parent", ttm_income),
        ("ttm_adjusted_net_income_parent", ttm_adjusted),
    ):
        if value is not None and ttm_statement is not None:
            setattr(
                result,
                attr,
                MetricValue(
                    value=value,
                    status=MetricStatus.DERIVED,
                    as_of=ttm_statement.period_end,
                    published_at=ttm_statement.published_at,
                    period="TTM",
                    source=ttm_statement.source,
                    currency=ttm_statement.currency,
                    note=(
                        "四个连续季度合计"
                        if complete_quarter_ttm
                        else "季度不足，使用最新已披露年报"
                    ),
                ),
            )

    price = current_price.value
    if _positive(price) and _positive(book_value_per_share):
        result.pb = MetricValue(
            value=price / book_value_per_share,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            published_at=latest.published_at,
            period="current_price/latest_public_statement",
            source=f"{current_price.source}+{latest.source}",
            note="现价/每股净资产",
        )
    elif latest.parent_equity is not None and latest.parent_equity <= 0:
        result.pb = MetricValue(
            status=MetricStatus.NOT_MEANINGFUL,
            note="归母净资产非正",
        )

    ttm_diluted_eps = _sum_last(ttm_quarters, "diluted_eps")
    if ttm_diluted_eps is None and latest_annual is not None:
        ttm_diluted_eps = latest_annual.diluted_eps
    if _positive(price) and _positive(ttm_diluted_eps):
        result.pe_ttm = MetricValue(
            value=price / ttm_diluted_eps,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            published_at=(
                ttm_statement.published_at if ttm_statement else None
            ),
            period="TTM",
            source=f"{current_price.source}+{ttm_statement.source}",
            note="现价/TTM稀释EPS",
        )
    elif _positive(price) and _positive(shares) and _positive(ttm_income):
        result.pe_ttm = MetricValue(
            value=price * shares / ttm_income,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            published_at=(
                ttm_statement.published_at if ttm_statement else None
            ),
            period="TTM",
            source=f"{current_price.source}+{ttm_statement.source}",
            note="现价×期末股本/TTM归母净利润",
        )
    elif ttm_income is not None and ttm_income <= 0:
        result.pe_ttm = MetricValue(
            status=MetricStatus.NOT_MEANINGFUL,
            note="TTM归母净利润非正",
        )

    prior_equity = None
    for statement in reversed(available):
        if statement.period_end >= latest.period_end:
            continue
        delta_days = (latest.period_end - statement.period_end).days
        if 330 <= delta_days <= 400 and statement.parent_equity is not None:
            prior_equity = statement.parent_equity
            break
    average_equity = latest.average_parent_equity
    if (
        average_equity is None
        and latest.parent_equity is not None
        and prior_equity is not None
    ):
        average_equity = (latest.parent_equity + prior_equity) / 2.0
    if _positive(ttm_income) and _positive(average_equity):
        result.roe_ttm = MetricValue(
            value=ttm_income / average_equity * 100.0,
            status=MetricStatus.DERIVED,
            as_of=latest.period_end,
            published_at=latest.published_at,
            period="TTM",
            source=latest.source,
            note="TTM归母净利润/平均归母净资产",
        )

    if (
        result.pe_ttm.value
        and result.pb.value
        and result.roe_ttm.value is not None
    ):
        implied = result.pb.value / result.pe_ttm.value * 100.0
        gap = abs(implied - result.roe_ttm.value)
        if gap > 5.0:
            result.roe_ttm.alternatives.append(
                {
                    "value": implied,
                    "method": "PB/PE cross-check",
                    "gap_percentage_points": gap,
                }
            )

    if (
        result.ttm_dividend_per_share.value is not None
        and _positive(price)
    ):
        status = (
            MetricStatus.KNOWN_ZERO
            if result.ttm_dividend_per_share.value == 0
            else MetricStatus.DERIVED
        )
        result.dividend_yield = MetricValue(
            value=result.ttm_dividend_per_share.value / price * 100.0,
            status=status,
            as_of=current_price.as_of,
            period="TTM",
            source=(
                f"{result.ttm_dividend_per_share.source}+{current_price.source}"
            ),
            note="TTM每股分红/现价",
        )

    _populate_growth(result, quarters, annuals)
    return result


def _populate_growth(
    result: CompanyFundamentals,
    quarters: list[FinancialStatementSnapshot],
    annuals: list[FinancialStatementSnapshot],
) -> None:
    fields = (
        ("revenue", "revenue"),
        ("net_income", "net_income_parent"),
        ("adjusted_net_income", "adjusted_net_income_parent"),
    )
    if quarters:
        current = quarters[-1]
        previous = quarters[-2] if len(quarters) >= 2 else None
        year_ago = next(
            (
                item
                for item in reversed(quarters[:-1])
                if 330 <= (current.period_end - item.period_end).days <= 400
            ),
            None,
        )
        source = current.source
        if (
            previous is not None
            and 55 <= (current.period_end - previous.period_end).days <= 125
        ):
            for key, field in fields:
                result.growth[f"{key}_qoq"] = _growth(
                    getattr(current, field),
                    getattr(previous, field),
                    current.period_end,
                    previous.period_end,
                    source,
                )
        if year_ago is not None:
            for key, field in fields:
                result.growth[f"{key}_yoy"] = _growth(
                    getattr(current, field),
                    getattr(year_ago, field),
                    current.period_end,
                    year_ago.period_end,
                    source,
                )

    regular_eight = _regular_quarters(quarters, 8)
    if regular_eight:
        current_four = regular_eight[-4:]
        prior_four = regular_eight[-8:-4]
        for key, field in (
            ("revenue_ttm_yoy", "revenue"),
            ("net_income_ttm_yoy", "net_income_parent"),
        ):
            current_sum = _sum_last(current_four, field)
            prior_sum = _sum_last(prior_four, field)
            result.growth[key] = _growth(
                current_sum,
                prior_sum,
                current_four[-1].period_end,
                prior_four[-1].period_end,
                current_four[-1].source,
            )
    elif len(annuals) >= 2:
        current_annual = annuals[-1]
        prior_annual = next(
            (
                item
                for item in reversed(annuals[:-1])
                if 330
                <= (current_annual.period_end - item.period_end).days
                <= 400
            ),
            None,
        )
        if prior_annual is None:
            return
        for key, field in fields:
            growth_key = f"{key}_yoy"
            if growth_key not in result.growth:
                result.growth[growth_key] = _growth(
                    getattr(current_annual, field),
                    getattr(prior_annual, field),
                    current_annual.period_end,
                    prior_annual.period_end,
                    current_annual.source,
                )
        for key, field in (
            ("revenue_ttm_yoy", "revenue"),
            ("net_income_ttm_yoy", "net_income_parent"),
        ):
            result.growth[key] = _growth(
                getattr(current_annual, field),
                getattr(prior_annual, field),
                current_annual.period_end,
                prior_annual.period_end,
                current_annual.source,
            )


def weighted_median(values: list[tuple[float, float]]) -> Optional[float]:
    valid = sorted((value, weight) for value, weight in values if weight > 0)
    if not valid:
        return None
    total = sum(weight for _, weight in valid)
    running = 0.0
    for value, weight in valid:
        running += weight
        if running >= total / 2:
            return value
    return median(value for value, _ in valid)


def derive_fund_profile(
    profile: FundProfile,
    *,
    current_price: MetricValue,
) -> FundProfile:
    result = profile.copy(deep=True)
    price = current_price.value
    nav = result.nav_per_unit.value
    if _positive(price) and _positive(nav):
        premium = (price - nav) / nav * 100.0
        result.premium_discount_rate = MetricValue(
            value=premium,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            source=f"{current_price.source}+{result.nav_per_unit.source}",
            note="(收盘价-NAV)/NAV",
        )
        result.p_nav = MetricValue(
            value=price / nav,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            source=f"{current_price.source}+{result.nav_per_unit.source}",
        )
    dividend = result.ttm_dividend_per_unit.value
    if dividend is not None and _positive(price):
        status = MetricStatus.KNOWN_ZERO if dividend == 0 else MetricStatus.DERIVED
        result.dividend_yield = MetricValue(
            value=dividend / price * 100.0,
            status=status,
            as_of=current_price.as_of,
            period="TTM",
            source=f"{result.ttm_dividend_per_unit.source}+{current_price.source}",
        )
        result.distribution_yield = result.dividend_yield.copy(deep=True)
    ffo_per_unit = result.ffo_per_unit.value
    if _positive(price) and _positive(ffo_per_unit):
        result.p_ffo = MetricValue(
            value=price / ffo_per_unit,
            status=MetricStatus.DERIVED,
            as_of=current_price.as_of,
            period="TTM",
            source=f"{current_price.source}+{result.ffo_per_unit.source}",
        )
    result.top_holdings = sorted(
        result.top_holdings,
        key=lambda holding: (-holding.weight, holding.code),
    )[:10]
    result.top_holdings_weight = sum(item.weight for item in result.top_holdings)
    result.look_through = calculate_look_through(result.top_holdings)
    return result


def calculate_look_through(
    holdings: Iterable[FundHolding],
) -> dict[str, LookThroughMetric]:
    rows = list(holdings)
    result: dict[str, LookThroughMetric] = {}
    for name in ("pe_ttm", "pb"):
        denominator = 0.0
        covered = 0.0
        for holding in rows:
            metric = getattr(holding.fundamentals, name)
            if _positive(metric.value):
                covered += holding.weight
                denominator += holding.weight / metric.value
        value = covered / denominator if denominator > 0 else None
        result[name] = LookThroughMetric(
            value=MetricValue(
                value=value,
                status=MetricStatus.DERIVED if value is not None else MetricStatus.MISSING,
                note="按盈利/账面收益率聚合",
            ),
            covered_weight=covered,
        )

    earnings = sum(
        holding.weight / holding.fundamentals.pe_ttm.value
        for holding in rows
        if _positive(holding.fundamentals.pe_ttm.value)
        and _positive(holding.fundamentals.pb.value)
    )
    book = sum(
        holding.weight / holding.fundamentals.pb.value
        for holding in rows
        if _positive(holding.fundamentals.pe_ttm.value)
        and _positive(holding.fundamentals.pb.value)
    )
    roe_covered = sum(
        holding.weight
        for holding in rows
        if _positive(holding.fundamentals.pe_ttm.value)
        and _positive(holding.fundamentals.pb.value)
    )
    result["roe_ttm"] = LookThroughMetric(
        value=MetricValue(
            value=earnings / book * 100.0 if book > 0 else None,
            status=MetricStatus.DERIVED if book > 0 else MetricStatus.MISSING,
            note="穿透盈利/穿透净资产",
        ),
        covered_weight=roe_covered,
    )

    dividend_sum = 0.0
    dividend_covered = 0.0
    for holding in rows:
        metric = holding.fundamentals.dividend_yield
        if metric.value is not None:
            dividend_sum += holding.weight * metric.value
            dividend_covered += holding.weight
    result["dividend_yield"] = LookThroughMetric(
        value=MetricValue(
            value=dividend_sum / dividend_covered if dividend_covered else None,
            status=(
                MetricStatus.DERIVED
                if dividend_covered
                else MetricStatus.MISSING
            ),
            note="按有效持仓权重归一化",
        ),
        covered_weight=dividend_covered,
    )

    for growth_name in (
        "revenue_yoy",
        "revenue_qoq",
        "net_income_yoy",
        "net_income_qoq",
    ):
        values = []
        for holding in rows:
            growth = getattr(holding.fundamentals, growth_name)
            if growth.value_pct is not None:
                values.append((growth.value_pct, holding.weight))
        value = weighted_median(values)
        result[growth_name] = LookThroughMetric(
            value=MetricValue(
                value=value,
                status=MetricStatus.DERIVED if value is not None else MetricStatus.MISSING,
                note="成分股增长率加权中位数，非基金会计增长率",
            ),
            covered_weight=sum(weight for _, weight in values),
        )
    return result
