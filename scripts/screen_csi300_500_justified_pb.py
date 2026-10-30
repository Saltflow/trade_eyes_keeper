#!/usr/bin/env python3
'''CSI 300 + CSI 500 justified-PB screen and out-of-sample evaluation.

Universe: current constituents of CSI 300 and CSI 500 (index reference
universe, 2026-08-18) with point-in-time market bundles and quarterly
fundamentals in the reference dataset.  The reference PIT history covers
2020-08 -> 2026-08 (72 months), so the walk-forward contract is adapted
to 20 overlapping 9-month test windows (3-month step, 6-month boundary):
windows 0-15 (first 16) select the top 20, windows 16-19 (last four) are
evaluated out-of-sample.
'''

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config
from src.backtest.engine import FastEvaluator, WalkForwardManager, WindowSlice
from src.data.market_history import PointInTimeMarketStore
from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.instruments.calculations import derive_company_fundamentals
from src.instruments.models import MetricStatus, MetricValue
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    adjust_statement_shares,
)
from src.search.config import get_constraints
from src.search.workflow import _prepare_wf_evaluation_contexts
from src.strategy.api import StrategyMarketData, TradePlan

REFERENCE = ROOT / 'data' / 'reference_universe'
CONSTITUENTS_PATH = REFERENCE / 'index_constituents_20260818.json'
DATASET_ROOT = (
    REFERENCE
    / 'point_in_time_20260818'
    / 'partial_first_pass_20260822'
    / 'dataset'
)
INDUSTRY_PATH = REFERENCE / 'industry_classification_history_official_2020q4_2025h2.json'
RUNTIME_PIT_ROOT = Path('data/point_in_time')

KE_BUY = 0.10
KE_SELL = 0.05
GROWTH_HALF = 0.5
GROWTH_CAP = 0.0          # g = min(half of median annualized growth, 0%)
TEST_MONTHS = 9
STEP_MONTHS = 3
STATE_BOUNDARY_MONTHS = 6
N_WINDOWS = 20
RANKING_WINDOWS = 16
TOP_N = 20
BASKET_WEIGHT = 1.0 / TOP_N
PER_SYMBOL_CAP = 0.20
TOTAL_EXPOSURE_CAP = 1.0
EXCLUDED_INDUSTRIES = {"J66", "J67", "J68"}   # 银行/证券/保险
BENCHMARKS = ('510300', '510500')


def _load_pool() -> tuple[dict, set[str]]:
    '''Pool = CSI 300 union CSI 500 members with PIT statements.'''
    constituents = json.load(open(CONSTITUENTS_PATH, encoding='utf-8'))
    pool = {}
    for company in constituents['companies']:
        memberships = set(company.get('memberships') or [])
        if memberships & {'csi_300', 'csi_500'}:
            pool[company['code']] = company
    statements = {
        path.name.split('.statements.json')[0]
        for path in (DATASET_ROOT / 'fundamentals').glob('*.statements.json')
    }
    return constituents, pool, statements


def _window_geometry(common_dates):
    '''(test_start, test_end) for every window on the common calendar.'''
    end_exclusive = pd.Timestamp(common_dates[-1]).normalize()
    end_exclusive = end_exclusive + pd.Timedelta(days=1)
    horizon_months = STATE_BOUNDARY_MONTHS + TEST_MONTHS + (
        N_WINDOWS - 1
    ) * STEP_MONTHS
    horizon_start = end_exclusive - pd.DateOffset(months=horizon_months)
    windows = []
    for index in range(N_WINDOWS):
        test_start_date = horizon_start + pd.DateOffset(
            months=STATE_BOUNDARY_MONTHS + index * STEP_MONTHS
        )
        test_end_date = test_start_date + pd.DateOffset(months=TEST_MONTHS)
        windows.append((
            test_start_date.date(),
            (test_end_date - pd.Timedelta(days=1)).date(),
        ))
    market_end = end_exclusive.date() - pd.Timedelta(days=1)
    return windows, horizon_start.date(), market_end


def _window_return(dates, qfq, start, end):
    start_index = int(dates.searchsorted(pd.Timestamp(start), side='left'))
    end_index = int(dates.searchsorted(pd.Timestamp(end), side='right')) - 1
    if start_index < 0 or end_index <= start_index or end_index >= len(dates):
        return None
    begin = qfq[start_index]
    finish = qfq[end_index]
    if not np.isfinite(begin) or not np.isfinite(finish) or begin <= 0:
        return None
    return float(finish / begin - 1.0)


def _per_symbol_growth(bundle, windows, train_window_count=20, use_cap=False):
    '''Return (g, annualized volatility %) over the training windows.

    g = median window return annualized / 2; with use_cap the result is
    capped at 0% (the original screen), otherwise it stays uncapped so the
    g >= 2% / high-growth exclusions can be applied.
    '''
    prices = bundle.prices
    dates = pd.DatetimeIndex(pd.to_datetime(prices['date'], errors='coerce'))
    qfq = pd.to_numeric(prices['qfq_close'], errors='coerce').to_numpy()
    returns = []
    for start, end in windows[:train_window_count]:
        value = _window_return(dates, qfq, start, end)
        if value is not None:
            returns.append(value)
    if len(returns) < 8:
        return None
    median_return = float(np.median(returns))
    annualized = (1.0 + median_return) ** (12.0 / TEST_MONTHS) - 1.0
    g_value = annualized * GROWTH_HALF
    if use_cap:
        g_value = float(min(g_value, GROWTH_CAP))
    vol_pct = None
    if len(returns) > 2:
        vol_pct = float(
            np.std(returns, ddof=1) * np.sqrt(12.0 / TEST_MONTHS) * 100.0
        )
    return float(g_value), vol_pct


def _selection_features(dataset):
    '''Latest roe_ttm / book_yield row at or before the selection date.'''
    names = dataset.feature_names
    ri = names.index('roe_ttm')
    bi = names.index('book_yield')
    return ri, bi



def _selection_fundamentals(universe, bundles, store, selection_date):
    '''PIT roe_ttm and pb as of the selection date for one stock pool.

    Mirrors the quarterly dataset builder's per-row computation (statement
    share adjustment + company fundamentals at the contemporaneous raw close)
    but evaluates only the selection date, which is all the screen needs.
    '''
    result = {}
    for code in universe:
        try:
            statements = store.as_of(code, selection_date)
        except Exception as exc:  # noqa: BLE001
            print('[warn] statements unavailable %s: %s' % (code, exc))
            continue
        if not statements:
            continue
        prices = bundles[code].prices
        dates = pd.DatetimeIndex(pd.to_datetime(prices['date'], errors='coerce'))
        raw = pd.to_numeric(prices['raw_close'], errors='coerce').to_numpy()
        index = int(dates.searchsorted(pd.Timestamp(selection_date), side='right')) - 1
        if index < 0 or not np.isfinite(raw[index]) or raw[index] <= 0:
            continue
        statements = adjust_statement_shares(statements, bundles[code].actions, selection_date)
        company = derive_company_fundamentals(
            statements,
            current_price=MetricValue(
                value=float(raw[index]),
                status=MetricStatus.OBSERVED,
                as_of=selection_date,
                source='point_in_time_raw_close',
            ),
            evaluation_date=selection_date,
        )
        roe = company.roe_ttm.value if company.roe_ttm is not None else None
        pb = company.pb.value if company.pb is not None else None
        if (roe is None or pb is None or not np.isfinite(roe) or not np.isfinite(pb)):
            continue
        if pb <= 0:
            continue
        latest_publication = max(
            (item.published_at for item in statements if item.published_at),
            default=selection_date,
        )
        result[code] = (
            float(roe) / 100.0,
            float(pb),
            latest_publication,
        )
    return result


def _holdout_plan(fmd, window, g_by_code, universe):
    '''TradePlan for the fixed top-20 basket inside one test window.
    Entry at the first tradable day; Ke=5% sell band from the PIT panel.
    '''
    ss, se = window.test_start, window.test_end
    N = len(universe)
    T = se - ss
    prices = np.asarray(fmd.prices, dtype=np.float64)[ss:se]
    tradable = np.asarray(fmd.tradable, dtype=bool)[ss:se]
    names = tuple(fmd.fundamental_feature_names)
    ri = names.index('roe_ttm')
    bi = names.index('book_yield')
    features = np.asarray(fmd.fundamental_features, dtype=np.float64)[ss:se]
    mask = np.asarray(fmd.fundamental_availability_mask, dtype=bool)[ss:se]
    roe = features[:, :, ri] / 100.0
    book_yield = features[:, :, bi]
    valid = (
        mask[:, :, ri]
        & mask[:, :, bi]
        & np.isfinite(roe)
        & np.isfinite(book_yield)
        & (book_yield > 0.0)
    )
    growth = np.array([g_by_code.get(str(code), 0.0) for code in universe])
    growth = growth[None, :]
    pb = np.full((T, N), np.nan, dtype=np.float64)
    np.divide(1.0, book_yield, out=pb, where=valid)
    justified_sell = np.full((T, N), np.nan, dtype=np.float64)
    np.divide(roe - growth, KE_SELL - growth, out=justified_sell, where=valid)
    sell_band = valid & (pb > justified_sell)
    entry_events = np.zeros((T, N), dtype=bool)
    for column in range(N):
        rows = np.flatnonzero(tradable[:, column])
        if len(rows):
            entry_events[rows[0], column] = True
    entry_events = entry_events & ~sell_band
    target_weights = np.full((T, N), BASKET_WEIGHT, dtype=np.float32)
    conviction = np.where(entry_events, 1.0, 0.0).astype(np.float32)
    execution = {
        'model': 'target_weight',
        'per_symbol_cap': PER_SYMBOL_CAP,
        'total_exposure_cap': TOTAL_EXPOSURE_CAP,
        'buy_price_model': 'max_high_t_minus_1_t_t_plus_1',
        'sell_price_model': 'trigger_day_low',
    }
    date_ordinals = None
    if getattr(fmd, 'date_ordinals', None) is not None:
        date_ordinals = np.asarray(fmd.date_ordinals, dtype=np.int64)[ss:se]
    return TradePlan(
        buy_signals=entry_events.copy(),
        sell_signals=sell_band.copy(),
        buy_priority=np.where(entry_events, 1.0, -np.inf).astype(np.float32),
        sell_priority=np.where(sell_band, 1.0, -np.inf).astype(np.float32),
        buy_cash_limit=0.0,
        sell_cash_limit=0.0,
        warmup_rows=1,
        dates=list(fmd.dates[ss:se]),
        symbols=list(universe),
        execution=execution,
        strategy_metadata={
            'strategy_id': 'fixed_top20_justified_pb',
            'ke_buy': KE_BUY,
            'ke_sell': KE_SELL,
            'top_n': TOP_N,
            'basket_weight': BASKET_WEIGHT,
        },
        entry_events=entry_events,
        exit_events=sell_band,
        force_exit_signals=np.zeros_like(entry_events, dtype=bool),
        conviction=conviction,
        target_weights=target_weights,
        date_ordinals=date_ordinals,
    )


_PROGRESS_LOG = Path('tmp_smoke/screen_progress.log')


def _progress(message: str) -> None:
    _PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _PROGRESS_LOG.open('a', encoding='utf-8') as handle:
        handle.write(message + '\n')
    print(message, flush=True)


def _raw_close_on(dates, raw, feature_date):
    '''Raw close at or before a feature date (the anchor price).'''
    index = int(dates.searchsorted(pd.Timestamp(feature_date), side='right')) - 1
    if index < 0 or index >= len(raw):
        return None
    value = float(raw[index])
    return value if np.isfinite(value) and value > 0 else None


def _nav_stats(nav_series, held_fractions):
    '''Return/max-drawdown/Sharpe of one daily NAV path (final=1.0 at start).'''
    nav = np.asarray(nav_series, dtype=np.float64)
    if len(nav) < 2 or not np.isfinite(nav).all():
        return {'return_pct': 0.0, 'max_drawdown_pct': 0.0,
                'sharpe_ratio': 0.0, 'avg_position_pct': 0.0,
                'excess_vs_strongest_pct': 0.0, 'strongest_benchmark': ''}
    total_return = float((nav[-1] / nav[0] - 1.0) * 100.0)
    drawdown_series = nav / np.maximum.accumulate(nav) - 1.0
    max_drawdown = float(drawdown_series.min() * 100.0)
    returns = np.diff(nav) / nav[:-1]
    returns = returns[np.isfinite(returns)]
    sharpe = 0.0
    if len(returns) > 5 and np.std(returns, ddof=1) > 1e-10:
        sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252))
    avg_position = (float(np.mean(held_fractions) * 100.0)
                    if held_fractions is not None and len(held_fractions) else 0.0)
    return {'return_pct': round(total_return, 2),
            'max_drawdown_pct': round(max_drawdown, 2),
            'sharpe_ratio': round(sharpe, 4),
            'avg_position_pct': round(avg_position, 1),
            'excess_vs_strongest_pct': 0.0,
            'strongest_benchmark': ''}


def _qfq_window_simulation(top_codes, top, bundles, dataset, growth, ws_date, we_date, runtime_store):
    '''One hold-out window on forward-adjusted prices with the Ke=5% exit rule.

    qfq series embed dividend and split adjustments, so corporate-action
    scheduling is not needed; commissions and reinvestment are not modelled.
    '''
    names = dataset.feature_names
    ri = names.index('roe_ttm')
    bi = names.index('book_yield')
    feature_rows = {}
    for row_index in range(len(dataset.symbols)):
        code = str(dataset.symbols[row_index])
        fd = dataset.feature_dates[row_index]
        if fd > we_date:
            continue
        if not (dataset.availability_mask[row_index, ri]
                and dataset.availability_mask[row_index, bi]):
            continue
        roe = float(dataset.values[row_index, ri]) / 100.0
        book_yield = float(dataset.values[row_index, bi])
        if not np.isfinite(roe) or not np.isfinite(book_yield) or book_yield <= 0:
            continue
        feature_rows.setdefault(code, []).append((fd, roe, book_yield))
    per_code = {}
    common_dates = None
    for code in top_codes:
        bundle = bundles[code]
        dates = pd.DatetimeIndex(pd.to_datetime(bundle.prices['date'], errors='coerce'))
        qfq = pd.to_numeric(bundle.prices['qfq_close'], errors='coerce').to_numpy()
        raw = pd.to_numeric(bundle.prices['raw_close'], errors='coerce').to_numpy()
        mask = (dates >= pd.Timestamp(ws_date)) & (dates <= pd.Timestamp(we_date))
        item = {'dates': dates[mask], 'qfq': qfq[mask], 'raw': raw[mask]}
        item['features'] = sorted(feature_rows.get(code, []), key=lambda value: value[0])
        per_code[code] = item
        if common_dates is None:
            common_dates = item['dates']
        else:
            common_dates = common_dates.intersection(item['dates'])
    if common_dates is None or len(common_dates) < 2:
        return None, {}, 0
    common_dates = common_dates.sort_values()
    n = max(1, len(top_codes))
    weight = 1.0 / n
    nav_daily = np.zeros(len(common_dates), dtype=np.float64)
    held_fractions = np.zeros(len(common_dates), dtype=np.float64)
    trades = 0
    for code in top_codes:
        item = per_code[code]
        qfq = pd.Series(item['qfq'], index=item['dates']).reindex(common_dates).ffill()
        raw = pd.Series(item['raw'], index=item['dates']).reindex(common_dates).ffill()
        growth_value = growth.get(code, 0.0)
        entry_price = None
        exit_price = None
        entered = False
        for day_index, day in enumerate(common_dates):
            price = float(qfq.iloc[day_index])
            pb_daily = None
            j2 = None
            for (fd, roe, book_yield) in item['features']:
                if fd <= day.date():
                    anchor_price = _raw_close_on(item['dates'], item['raw'], fd)
                    if anchor_price is not None and book_yield > 0:
                        book_per_share = anchor_price * book_yield
                        price_day = float(raw.iloc[day_index])
                        if np.isfinite(price_day) and price_day > 0:
                            pb_daily = price_day / book_per_share
                            j2 = (roe - growth_value) / (KE_SELL - growth_value)
                else:
                    break
            if not np.isfinite(price) or price <= 0:
                continue
            if entry_price is None:
                entry_price = price
                entered = True
            if exit_price is None and pb_daily is not None and j2 is not None and pb_daily > j2:
                exit_price = price
                trades += 1
            if entry_price is not None:
                ratio = (exit_price if exit_price is not None else price) / entry_price
                nav_daily[day_index] += weight * ratio if np.isfinite(ratio) else weight
                held_fractions[day_index] += weight
    nav_daily = np.maximum(nav_daily, 1e-12)
    nav_daily = nav_daily / nav_daily[0]
    # benchmarks on the same grid
    benchmark_returns = {}
    for benchmark in BENCHMARKS:
        bundle = runtime_store.read(benchmark)
        if bundle is None:
            continue
        dates = pd.DatetimeIndex(pd.to_datetime(bundle.prices['date'], errors='coerce'))
        qfq = pd.to_numeric(bundle.prices['qfq_close'], errors='coerce')
        series = pd.Series(qfq.to_numpy(), index=dates).reindex(common_dates).ffill()
        values = series.to_numpy(dtype=np.float64)
        if len(values) < 2 or not np.isfinite(values[[0, -1]]).all() or values[0] <= 0:
            continue
        benchmark_returns[benchmark] = round((values[-1] / values[0] - 1.0) * 100.0, 2)
    days = len(common_dates)
    benchmark_returns['risk_free'] = round(
        ((1.0 + 0.02) ** (days / 252.0) - 1.0) * 100.0, 2
    )
    stats = _nav_stats(nav_daily, held_fractions)
    strongest = max(benchmark_returns, key=benchmark_returns.get)
    stats['excess_vs_strongest_pct'] = round(
        stats['return_pct'] - benchmark_returns[strongest], 2
    )
    stats['strongest_benchmark'] = strongest
    return stats, benchmark_returns, trades


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--output-root', type=Path, default=
        ROOT / 'data' / 'analysis' / 'justified_pb_csi300500'
    )
    parser.add_argument('--selection-date', type=str, default=None,
        help='screen as-of date (default: end of the first 16 windows)')
    parser.add_argument('--train-windows', type=int, default=N_WINDOWS,
        help='training windows for volatility/growth (default: all 20)')
    parser.add_argument('--growth-mode', choices=('half', 'capped_zero'),
        default='half',
        help='g = annualized median / 2 (default), or min(annualized/2, 0%)')
    parser.add_argument('--vol-exclude-top', type=int, default=20,
        help='exclude the N most volatile training candidates')
    parser.add_argument('--g-min', type=float, default=0.02,
        help='exclude candidates with g below this value')
    parser.add_argument('--g-exclude-top', type=int, default=20,
        help='exclude the N candidates with the highest g')
    parser.add_argument('--pb-exclude-low', type=int, default=20,
        help='exclude the N candidates with the lowest PB')
    parser.add_argument('--ke-sell', type=float, default=0.06,
        help='sell-side cost of equity for the exit band')
    args = parser.parse_args()
    top_n = max(1, int(args.top_n))
    global KE_SELL
    KE_SELL = float(args.ke_sell)

    constituents, pool, statements = _load_pool()
    _progress('stage: pool loaded')
    print('pool (CSI300 union CSI500): %d' % len(pool))
    print('pool codes with PIT statements: %d' % len(set(pool) & statements))

    market_store = PointInTimeMarketStore(DATASET_ROOT)
    codes = sorted(set(pool) & statements)
    bundles = {}
    _progress('stage: loading bundles')
    for code in codes:
        bundle = market_store.read(code)
        if bundle is None:
            continue
        bundles[code] = bundle
    _progress('stage: bundles loaded: %d' % len(bundles))

    all_dates = [
        pd.DatetimeIndex(pd.to_datetime(bundle.prices['date'], errors='coerce'))
        for bundle in bundles.values() if len(bundle.prices)
    ]
    if not all_dates:
        raise SystemExit('no market data in the reference dataset')
    common = all_dates[0]
    for item in all_dates[1:]:
        common = common.intersection(item)
    common = common.sort_values()
    print('common calendar: %s -> %s (%d rows)' % (
        common[0], common[-1], len(common)
    ))
    windows, horizon_start, market_end = _window_geometry(common)
    first_start, last_end = windows[0][0], windows[-1][1]
    print('geometry: data %s -> %s; 20 windows; test %s -> %s' % (
        horizon_start, market_end, first_start, last_end
    ))

    print('note: reference bundles carry raw adjustment factors only;')
    print('      the hold-out evaluation uses qfq (adjusted) price series')
    _progress('stage: common calendar + universe filter')
    universe = [
        code
        for code, bundle in bundles.items()
        if str(pd.Timestamp(bundle.prices['date'].iloc[0]).date()) <= str(first_start)
        and str(pd.Timestamp(bundle.prices['date'].iloc[-1]).date()) >= str(last_end)
    ]
    print('universe (full 20-window coverage): %d' % len(universe))

    _progress('stage: per-symbol growth (16 ranking windows)')
    growth = {}
    volatility = {}
    growth_missing = []
    train_count = max(1, min(int(args.train_windows), N_WINDOWS))
    for code in universe:
        value = _per_symbol_growth(
            bundles[code], windows,
            train_window_count=train_count,
            use_cap=(args.growth_mode == 'capped_zero'),
        )
        if value is None:
            growth_missing.append(code)
        else:
            growth[code], volatility[code] = value
    _progress('growth estimated: %d / %d (training windows: %d)' % (
        len(growth), len(universe), train_count
    ))

    selection_date = date.fromisoformat(
        args.selection_date or windows[-1][1].isoformat()
    )
    print('selection date: %s' % selection_date)

    _progress('stage: selection fundamentals (PIT as of %s)' % selection_date)
    fundamental_store = PointInTimeFundamentalStore(DATASET_ROOT)
    latest = _selection_fundamentals(
        universe, bundles, fundamental_store, selection_date
    )
    print('fundamentals at selection date: %d symbols' % len(latest))

    industry = IndustryClassificationHistoryStore(INDUSTRY_PATH).labels_as_of(
        selection_date, universe
    )
    print('industry labels at selection date: %d' % len(industry))

    _progress('stage: screen + rank')
    candidates = []
    for code in universe:
        entry = latest.get(code)
        if entry is None:
            continue
        roe, pb, as_of = entry
        g = growth.get(code)
        if g is None:
            continue
        if g >= KE_BUY:
            continue
        if roe <= g:
            continue
        justified = (roe - g) / (KE_BUY - g)
        if not np.isfinite(justified) or justified <= 0:
            continue
        if not np.isfinite(pb) or pb <= 0:
            continue
        label = industry.get(code)
        industry_code = label.industry_code if label is not None else ''
        candidates.append({
            'code': code,
            'name': pool[code].get('name', ''),
            'indices': sorted(set(pool[code].get('memberships', [])) & {'csi_300', 'csi_500'}),
            'industry_code': industry_code,
            'industry_name': label.industry_name if label is not None else '',
            'roe_pct': roe * 100.0,
            'pb': pb,
            'g_pct': g * 100.0,
            'justified_pb': justified,
            'discount': justified / pb,
            'vol_pct': volatility.get(code),
            'as_of': str(as_of),
        })
    candidates = [
        item for item in candidates
        if item['industry_code'] not in EXCLUDED_INDUSTRIES
    ]
    print('screen survivors before exclusions: %d' % len(candidates))
    if args.vol_exclude_top > 0:
        candidates.sort(key=lambda item: -(item.get('vol_pct') or 0.0))
        removed_vol = [item['code'] for item in candidates[:args.vol_exclude_top]]
        candidates = candidates[args.vol_exclude_top:]
        print('excluded 波动最大 top-%d: %s' % (
            args.vol_exclude_top, ','.join(removed_vol[:16])
        ))
    if args.g_min and args.g_min > 0:
        before = len(candidates)
        candidates = [
            item for item in candidates
            if item['g_pct'] >= args.g_min * 100.0
        ]
        print('excluded g<%.0f%%: %d names' % (args.g_min * 100.0, before - len(candidates)))
    if args.g_exclude_top > 0:
        candidates.sort(key=lambda item: (-item['g_pct'], item['code']))
        removed_g = [item['code'] for item in candidates[:args.g_exclude_top]]
        candidates = candidates[args.g_exclude_top:]
        print('excluded g最高 top-%d: %s' % (
            args.g_exclude_top, ','.join(removed_g[:16])
        ))
    if args.pb_exclude_low > 0:
        candidates.sort(key=lambda item: (item['pb'], item['code']))
        removed_pb = [item['code'] for item in candidates[:args.pb_exclude_low]]
        candidates = candidates[args.pb_exclude_low:]
        print('excluded PB最低 top-%d: %s' % (
            args.pb_exclude_low, ','.join(removed_pb[:16])
        ))
    candidates.sort(
        key=lambda item: (-item['discount'], -item['roe_pct'], item['code'])
    )
    top = candidates[:top_n]
    if len(top) < top_n:
        print('[warn] only %d names pass the screen (asked %d)' % (len(top), top_n))
    print()
    _progress('stage: top-%d selected' % top_n)
    print('=' * 96)
    growth_label = ('g = min(median annualized/2, 0)%') if args.growth_mode == 'capped_zero' else ('g = median annualized/2')
    print('TOP %d | Ke=10%% buy screen | %s' % (top_n, growth_label))
    print('selection date: %s | excluded industries: %s' % (
        selection_date, ', '.join(sorted(EXCLUDED_INDUSTRIES))
    ))
    print('=' * 96)
    print('%-7s %-10s %-9s %-12s %7s %7s %7s %7s %8s %6s' % (
        'code', 'name', 'index', 'industry', 'roe%', 'pb', 'g%', 'jPB', 'disc', 'w%'
    ))
    for rank, item in enumerate(top, 1):
        print('%-7s %-10s %-9s %-12s %7.2f %7.2f %7.2f %7.2f %8.2f %6.2f' % (
            item['code'], (item['name'] or '')[:8],
            '/'.join('300' if key == 'csi_300' else '500' for key in item['indices']),
            item['industry_name'][:10] or '-',
            item['roe_pct'], item['pb'], item['g_pct'],
            item['justified_pb'], item['discount'], 100.0 * BASKET_WEIGHT,
        ))

    top_codes = [item['code'] for item in top]

# ---- hold-out evaluation on the last four windows (qfq-based) ----
# The reference PIT store stores only raw adjustment factors (no resolved
# cash/share actions), so the execution engine rejects those bundles.  The
# basket is therefore evaluated on forward-adjusted (qfq) prices: dividends
# and splits are embedded in the adjustment, no commissions and no reinvestment.
    top_codes = [item['code'] for item in top]
    runtime_store = PointInTimeMarketStore(RUNTIME_PIT_ROOT)
    _progress('stage: top-20 PIT dataset build (qfq holdout)')
    dataset = QuarterlyPricingDatasetBuilder(
        DATASET_ROOT, market='a_share'
    ).build(symbols=tuple(top_codes))
    print('top-20 PIT dataset rows: %d' % len(dataset.symbols))
    _progress('stage: qfq holdout evaluation' + ' (%d names)' % len(top_codes))

    wf_windows = windows[RANKING_WINDOWS:]
    rows = []
    for index, (ws_date, we_date) in enumerate(wf_windows):
        stats, benchmark_returns, stats_trades = _qfq_window_simulation(
            top_codes, top, bundles, dataset, growth, ws_date, we_date,
            runtime_store
        )
        rows.append({
            'window_index': RANKING_WINDOWS + index,
            'test_start': str(ws_date),
            'test_end': str(we_date),
            'return_pct': stats['return_pct'],
            'max_drawdown_pct': stats['max_drawdown_pct'],
            'sharpe_ratio': stats['sharpe_ratio'],
            'avg_position_pct': stats['avg_position_pct'],
            'total_trades': stats_trades,
            'benchmark_returns': benchmark_returns,
            'excess_vs_strongest_pct': stats['excess_vs_strongest_pct'],
            'strongest_benchmark': stats['strongest_benchmark'],
        })

    print()
    print('=' * 96)
    print('TOP-20 basket in the last four windows (Ke=%.0f%% sell signal, qfq prices)'
          % (KE_SELL * 100.0))
    print('=' * 96)
    print('%-4s %-12s %-12s %9s %8s %8s %9s' % (
        'win', 'test_start', 'test_end', 'return%', 'maxdd%', 'sharpe', 'excess%'
    ))
    for row in rows:
        print('%-4s %-12s %-12s %9.2f %8.2f %8.4f %9.2f' % (
            row['window_index'], row['test_start'], row['test_end'],
            row['return_pct'], row['max_drawdown_pct'],
            row['sharpe_ratio'], row['excess_vs_strongest_pct'],
        ))
    if rows:
        returns = [row['return_pct'] for row in rows]
        drawdowns = [row['max_drawdown_pct'] for row in rows]
        sharpes = [row['sharpe_ratio'] for row in rows]
        keys = sorted({k for row in rows for k in row['benchmark_returns']})
        wins = {
            key: sum(
                1
                for row in rows
                if row['return_pct'] > float(row['benchmark_returns'].get(key, -1e9))
            )
            for key in keys
        }
        print()
        print('  4窗: 均值收益 %.2f%% | 中位 %.2f%% | 最差回撤 %.2f%% | 平均Sharpe %.4f' % (
            float(np.mean(returns)), float(np.median(returns)),
            float(min(drawdowns)), float(np.mean(sharpes))
        ))
        print('  胜基准: %s' % json.dumps(wins))
        print('  各窗基准: %s' % json.dumps(
            {str(row['window_index']): row['benchmark_returns'] for row in rows},
            ensure_ascii=False,
        ))
    output_dir = args.output_root / datetime.now(timezone.utc).astimezone().strftime(
        '%Y%m%d_%H%M%S'
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'contract': {'ke_buy': KE_BUY, 'ke_sell': KE_SELL, 'growth_half': GROWTH_HALF,
                     'growth_cap': GROWTH_CAP, 'test_months': TEST_MONTHS,
                     'step_months': STEP_MONTHS, 'windows': N_WINDOWS,
                     'ranking_windows': RANKING_WINDOWS, 'top_n': top_n,
                     'basket_weight': BASKET_WEIGHT,
                     'growth_mode': args.growth_mode,
                     'train_windows': train_count,
                     'vol_exclude_top': args.vol_exclude_top,
                     'g_min': args.g_min,
                     'g_exclude_top': args.g_exclude_top,
                     'pb_exclude_low': args.pb_exclude_low,
                     'excluded_industries': sorted(EXCLUDED_INDUSTRIES)},
        'selection_date': str(selection_date),
        'geometry': {'horizon_start': str(horizon_start), 'market_end': str(market_end),
                     'data_start': str(common[0].date()), 'window_tests': [
                         [str(a), str(b)] for a, b in windows]},
        'universe': {'pool': len(pool), 'with_statements': len(set(pool) & statements),
                     'bundles': len(bundles), 'evaluable': len(universe),
                     'growth_estimated': len(growth), 'screen_candidates': len(candidates),
                     'top_n': len(top)},
        'fundamentals_at_selection': len(latest),
        'industry_labels_at_selection': len(industry),
        'top20': top,
        'holdout_windows': rows,
    }
    (output_dir / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    import csv
    with (output_dir / 'top20.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            'rank', 'code', 'name', 'indices', 'industry_code', 'industry_name',
            'roe_pct', 'pb', 'g_pct', 'vol_pct', 'justified_pb', 'discount', 'as_of'
        ])
        writer.writeheader()
        for rank, item in enumerate(top, 1):
            item = dict(item)
            item['rank'] = rank
            item['indices'] = '/'.join(item['indices'])
            writer.writerow(item)
    if rows:
        with (output_dir / 'holdout_windows.csv').open('w', encoding='utf-8', newline='') as h:
            writer = csv.DictWriter(h, fieldnames=[
                'window_index', 'test_start', 'test_end', 'return_pct',
                'max_drawdown_pct', 'sharpe_ratio', 'avg_position_pct',
                'total_trades', 'excess_vs_strongest_pct', 'strongest_benchmark'
            ])
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row[k] for k in writer.fieldnames})
    print()
    print('artifacts saved under: %s' % output_dir)
    return 0


def make_enricher(dataset):
    from src.strategy.context_enrichment import make_historical_dataset_enricher
    return make_historical_dataset_enricher(
        dataset, required_feature_names=('roe_ttm', 'book_yield')
    )


if __name__ == "__main__":
    raise SystemExit(main())