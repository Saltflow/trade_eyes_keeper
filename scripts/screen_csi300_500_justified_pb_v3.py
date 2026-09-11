#!/usr/bin/env python3
'''CSI 300 + CSI 500 value-momentum screen v3 (corrected model).

Model fixes vs v2:
  1. unified Ke_implied = g + (ROE - g) / PB: buy when Ke_implied >= 10%,
     sell when Ke_implied < 6%.  The old threshold form PB > (ROE-g)/(Ke-g)
     silently flips its inequality direction once g > Ke, which produced the
     g >= 6% unconditional-sell artifact.
  2. g is now a fundamental growth estimate: 5-year book-value CAGR, floored
     at 0% and capped at 6% (fallback 3%).  Historical price returns are
     kept as a separately-labelled momentum factor (band-pass: >= 2%, and
     the top-20 momentum names are excluded).
  3. ROE is normalized: median of the trailing annual ROE over up to 5 fiscal
     years as of the selection date (point-in-time).
  4. rolling walk-forward: every window re-selects the basket using only data
     published before that window starts (momentum/volatility from windows
     ending at or before the selection date; statements published by then).
  5. five benchmark series per window: 510300 / 510500 / eligible universe
     equal weight / naive ROE+PB equal weight / naive low-volatility equal
     weight (all bought at the window start, no sell rule).

Evaluation uses forward-adjusted (qfq) prices: the reference PIT store only
carries raw adjustment factors, so no commission/cash-reinvestment model.
'''

from __future__ import annotations

import argparse
import csv
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
from src.data.market_history import PointInTimeMarketStore
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.instruments.point_in_time import PointInTimeFundamentalStore

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
KE_SELL = 0.06
GROWTH_YEARS = 5
ROE_NORM_YEARS = 5
GROWTH_CAP = 0.06
GROWTH_FLOOR = 0.0
GROWTH_FALLBACK = 0.03
MOMENTUM_HALF = 0.5
MOMENTUM_MIN_PCT = 2.0       # band-pass lower bound (percent)
TEST_MONTHS = 9
STEP_MONTHS = 3
STATE_BOUNDARY_MONTHS = 6
N_WINDOWS = 20
TOP_N = 20
VOL_EXCLUDE_TOP = 20
MOM_EXCLUDE_TOP = 20
PB_LOW_TOP = 20
MIN_MOMENTUM_WINDOWS = 6
EXCLUDED_INDUSTRIES = {'J66', 'J67', 'J68'}   # bank / securities / insurance
BENCHMARKS = ('510300', '510500')


def _load_pool() -> tuple[dict, set[str]]:
    '''Pool = CSI 300 union CSI 500 members.'''
    constituents = json.load(open(CONSTITUENTS_PATH, encoding='utf-8'))
    pool = {}
    for company in constituents['companies']:
        if set(company.get('memberships') or []) & {'csi_300', 'csi_500'}:
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


def _annual_statement_series(store, code):
    '''Fiscal-year-end records: (year, published_at, roe_pct, equity, shares).
    Uses the latest revision of each statement period and only records whose
    publication date is known (point-in-time).'''
    rows = []
    try:
        records = store.read_all(code)
    except Exception:
        return rows
    by_period = {}
    for item in records:
        period_end = getattr(item, 'period_end', None)
        published = getattr(item, 'published_at', None)
        if period_end is None or published is None or period_end.month != 12:
            continue
        net_income = getattr(item, 'net_income_parent', None)
        equity = getattr(item, 'average_parent_equity', None)
        if equity is None:
            equity = getattr(item, 'parent_equity', None)
        if net_income is None or equity is None or equity <= 0:
            continue
        shares = getattr(item, 'total_shares', None)
        if shares is None:
            shares = getattr(item, 'common_shares_outstanding', None)
        key = (period_end.year, str(getattr(item, 'period_type', '')))
        row = (published, float(net_income), float(equity),
               float(shares) if shares else None)
        current = by_period.get(key)
        if current is None or published > current[0]:
            by_period[key] = row
    for (year, _ptype), (published, net_income, equity, shares) in by_period.items():
        roe_pct = net_income / equity * 100.0
        rows.append((year, published, roe_pct, equity, shares))
    return sorted(rows, key=lambda row: row[0])


def _fundamentals_as_of(annuals, selection_date):
    '''(normalized ROE, fundamental g, book per share) as of the date.'''
    eligible = [
        row for row in annuals
        if row[1] <= selection_date
    ]
    if not eligible:
        return None
    recent = eligible[-ROE_NORM_YEARS:]
    roes = [row[2] for row in recent]
    if len(roes) < 2:
        return None
    nroe = float(np.median(roes)) / 100.0
    # Fundamental growth must be per-share: total parent equity CAGR would
    # count seasoned equity issuance as growth.  BVPS = equity / shares.
    bvps_rows = [
        (row[0], row[3] / row[4])
        for row in eligible
        if row[3] is not None and row[4] is not None and row[3] > 0 and row[4] > 0
    ]
    if len(bvps_rows) >= 2:
        first_year, first_bvps = bvps_rows[0]
        last_year, last_bvps = bvps_rows[-1]
        years = last_year - first_year
        if years >= 2 and first_bvps > 0 and last_bvps > 0:
            cagr = (last_bvps / first_bvps) ** (1.0 / years) - 1.0
            g_fund = float(min(max(cagr, GROWTH_FLOOR), GROWTH_CAP))
        else:
            g_fund = GROWTH_FALLBACK
    else:
        g_fund = GROWTH_FALLBACK
    latest = eligible[-1]
    book_per_share = None
    if latest[4] is not None and latest[4] > 0:
        book_per_share = float(latest[3] / latest[4])
    return nroe, g_fund, book_per_share


def _bvps_series(annuals, as_of):
    '''(year, BVPS) rows published at or before the as-of date.'''
    rows = []
    for row in annuals:
        if row[1] > as_of:
            continue
        if row[3] is None or row[4] is None or row[3] <= 0 or row[4] <= 0:
            continue
        rows.append((row[0], row[3] / row[4]))
    return rows


def _bvps_cagr(annuals, start_date, end_date):
    '''BVPS CAGR between the last annual before start and the last before end.'''
    start_rows = _bvps_series(annuals, start_date)
    end_rows = _bvps_series(annuals, end_date)
    if len(start_rows) < 1 or len(end_rows) < 2:
        return None, None
    start_year, start_bvps = start_rows[-1]
    end_year, end_bvps = end_rows[-1]
    years = end_year - start_year
    if years < 1 or start_bvps <= 0 or end_bvps <= 0:
        return None, None
    return (end_bvps / start_bvps) ** (1.0 / years) - 1.0, years


def _industry_map(path, selection_dates):
    '''symbol -> industry code as of each date (latest published record).'''
    observations = IndustryClassificationHistoryStore(path).read()
    result = {selection_date: {} for selection_date in selection_dates}
    for observation in observations:
        code = observation.symbol
        published = observation.published_at
        for selection_date in selection_dates:
            if published > selection_date:
                continue
            current = result[selection_date].get(code)
            if current is None or (published, observation.period_end) > current:
                result[selection_date][code] = (published, observation.period_end, observation.industry_code)
    return {
        selection_date: {
            code: value[2]
            for code, value in mapping.items()
        }
        for selection_date, mapping in result.items()
    }


def _series_on_grid(code, bundle, common_dates, precomp):
    '''qfq ratio path / raw price path / first tradable index on the grid.

    Universe codes carry every common-calendar date, so a searchsorted lookup
    is exact and no reindex/ffill is needed.
    '''
    item = precomp[code]
    dates = item['dates']
    qfq = item['qfq']
    raw = item['raw']
    positions = np.clip(
        dates.searchsorted(common_dates, side='right') - 1,
        0,
        len(dates) - 1,
    )
    qfq_grid = qfq[positions]
    raw_grid = raw[positions]
    first = 0
    limit = len(qfq_grid)
    while first < limit and (not np.isfinite(qfq_grid[first]) or qfq_grid[first] <= 0):
        first += 1
    if first >= limit:
        return None
    return {'ratio': qfq_grid / qfq_grid[first], 'raw': raw_grid, 'first': first}


def _compute_stats(nav):
    '''Return / max drawdown / Sharpe of a daily NAV path starting at 1.'''
    nav = np.asarray(nav, dtype=np.float64)
    if len(nav) < 2 or not np.isfinite(nav).all() or nav[0] <= 0:
        return {'return_pct': 0.0, 'max_drawdown_pct': 0.0, 'sharpe_ratio': 0.0}
    total_return = float((nav[-1] / nav[0] - 1.0) * 100.0)
    dd = nav / np.maximum.accumulate(nav) - 1.0
    max_dd = float(dd.min() * 100.0)
    returns = np.diff(nav) / nav[:-1]
    returns = returns[np.isfinite(returns)]
    sharpe = 0.0
    if len(returns) > 5 and np.std(returns, ddof=1) > 1e-10:
        sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252))
    return {'return_pct': round(total_return, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'sharpe_ratio': round(sharpe, 4)}


def _hold_nav(names, series_map, grid):
    '''Equal-weight buy-and-hold NAV (no sell rule).'''
    weight = 1.0 / max(1, len(names))
    nav = np.zeros(len(grid), dtype=np.float64)
    for code in names:
        item = series_map.get(code)
        if item is None:
            nav += weight
            continue
        if item['first'] >= len(grid):
            nav += weight
            continue
        path = np.full(len(grid), weight, dtype=np.float64)
        path[item['first']:] = weight * item['ratio'][item['first']:]
        nav += path
    if np.isfinite(nav[0]) and nav[0] > 0:
        nav = nav / nav[0]
    return nav


def _basket_nav(names, series_map, grid, model):
    '''Equal-weight basket; the Ke_implied sell rule when model is provided.'''
    weight = 1.0 / max(1, len(names))
    nav = np.zeros(len(grid), dtype=np.float64)
    trades = 0
    for code in names:
        item = series_map.get(code)
        if item is None:
            nav += weight
            continue
        first = item['first']
        ratio = item['ratio']
        if first >= len(ratio):
            nav += weight
            continue
        entry = float(ratio[first])
        exit_ratio = None
        exit_index = None
        params = model.get(code) if model else None
        if params is not None:
            g_fund, nroe, book_per_share = params
            for day_index in range(first, len(ratio)):
                raw_day = float(item['raw'][day_index])
                if (not np.isfinite(raw_day) or raw_day <= 0
                        or not book_per_share or book_per_share <= 0):
                    continue
                pb_daily = raw_day / book_per_share
                ke_implied = g_fund + (nroe - g_fund) / pb_daily
                if ke_implied < KE_SELL:
                    exit_ratio = float(ratio[day_index])
                    exit_index = day_index
                    trades += 1
                    break
        path = np.full(len(grid), weight, dtype=np.float64)
        if entry > 0:
            path[first:] = weight * (ratio[first:] / entry)
        if exit_ratio is not None and exit_index is not None:
            path[exit_index + 1:] = weight * (exit_ratio / entry)
        nav += path
    if np.isfinite(nav[0]) and nav[0] > 0:
        nav = nav / nav[0]
    return nav, trades


def _select_window(returns_by_code, fundamentals_by_code, pb_by_code,
                   volatility_by_code, codes, industry_map):
    '''Screen one walk-forward window (Ke_implied form + filters).
    Returns (ke10_codes, candidates, filters applied).'''
    ke10_all = []
    candidates = []
    for code in codes:
        stats = fundamentals_by_code.get(code)
        if stats is None:
            continue
        nroe, g_fund, book_per_share = stats
        pb = pb_by_code.get(code)
        if pb is None or not np.isfinite(pb) or pb <= 0:
            continue
        if nroe is None or nroe <= 0:
            continue
        ke_implied = g_fund + (nroe - g_fund) / pb
        if ke_implied < KE_BUY:
            continue
        discount10 = ((nroe - g_fund) / (KE_BUY - g_fund)) / pb
        assert discount10 >= 1.0 - 1e-9, (code, discount10, ke_implied)
        ke10_all.append(code)
        industry_code = industry_map.get(code) or ''
        if industry_code in EXCLUDED_INDUSTRIES:
            continue
        momentum = returns_by_code.get(code)
        if momentum is None:
            continue
        candidates.append({
            'code': code,
            'ke_implied_pct': ke_implied * 100.0,
            'discount10': discount10,
            'nroe_pct': nroe * 100.0,
            'g_fund_pct': g_fund * 100.0,
            'momentum_pct': momentum.get('momentum_pct'),
            'vol_pct': momentum.get('vol_pct'),
            'pb': pb,
            'industry_code': industry_code,
        })
    return ke10_all, candidates


_PROGRESS_PATH = Path('tmp_smoke/v3_progress.txt')


def _stage(message):
    _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PROGRESS_PATH.open('a', encoding='utf-8') as handle:
        handle.write(message + '\n')
    print(message, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--output-root', type=Path, default=
        ROOT / 'data' / 'analysis' / 'justified_pb_csi300500_v3'
    )
    parser.add_argument('--min-windows', type=int, default=MIN_MOMENTUM_WINDOWS,
        help='minimum prior windows required before a rolling selection')
    args = parser.parse_args()
    top_n = max(1, int(args.top_n))
    min_windows = max(4, int(args.min_windows))

    constituents, pool, statements = _load_pool()
    _stage('s1 pool')

    market_store = PointInTimeMarketStore(DATASET_ROOT)
    codes_all = sorted(set(pool) & statements)
    bundles = {}
    for code in codes_all:
        bundle = market_store.read(code)
        if bundle is not None:
            bundles[code] = bundle
    _stage('s2 bundles')

    # Use the longest available calendar as the master grid.  The strict
    # intersection of 663 provider calendars collapses to ~130 rows because of
    # a few outlier date sets, so per-window grids must follow one master
    # calendar; other names are mapped by carry-forward (last date <= grid day).
    def _calendar(bundle):
        return pd.DatetimeIndex(
            pd.to_datetime(bundle.prices['date'], errors='coerce')
        )
    mainstream = []
    for bundle in bundles.values():
        dates = _calendar(bundle)
        if len(dates) < 300:
            continue
        if dates[0] > pd.Timestamp('2020-10-01'):
            continue
        if dates[-1] > pd.Timestamp('2026-08-21'):
            continue
        mainstream.append((len(dates), dates))
    if not mainstream:
        mainstream = [
            (len(_calendar(bundle)), _calendar(bundle))
            for bundle in bundles.values()
        ]
    common = max(mainstream, key=lambda item: item[0])[1].sort_values()
    windows, horizon_start, market_end = _window_geometry(common)
    first_start, last_end = windows[0][0], windows[-1][1]
    print('geometry: data %s -> %s | 20 windows | test %s -> %s' % (
        common[0].date(), market_end, first_start, last_end)
    )

    universe = [
        code
        for code, bundle in bundles.items()
        if str(pd.Timestamp(bundle.prices['date'].iloc[0]).date()) <= str(first_start)
        and str(pd.Timestamp(bundle.prices['date'].iloc[-1]).date()) >= str(last_end)
    ]
    print('universe (full 20-window coverage): %d' % len(universe), flush=True)

    precomp = {}
    for code in universe:
        bundle = bundles[code]
        precomp[code] = {
            'dates': pd.DatetimeIndex(
                pd.to_datetime(bundle.prices['date'], errors='coerce')
            ),
            'qfq': pd.to_numeric(
                bundle.prices['qfq_close'], errors='coerce'
            ).to_numpy(),
            'raw': pd.to_numeric(
                bundle.prices['raw_close'], errors='coerce'
            ).to_numpy(),
        }
    return_signals = {
        code: precomp[code]['qfq']
        for code in universe
    }
    del return_signals
    _stage('s3 precomp')

    returns_by_code = {}
    for code in universe:
        bundle = bundles[code]
        dates = pd.DatetimeIndex(pd.to_datetime(bundle.prices['date'], errors='coerce'))
        qfq = pd.to_numeric(bundle.prices['qfq_close'], errors='coerce').to_numpy()
        values = []
        for start, end in windows:
            value = _window_return(dates, qfq, start, end)
            values.append(value if value is not None else np.nan)
        returns_by_code[code] = np.asarray(values, dtype=np.float64)

    fundamental_store = PointInTimeFundamentalStore(DATASET_ROOT)
    annual_cache = {code: _annual_statement_series(fundamental_store, code)
                   for code in universe}
    _stage('s4 annuals')

    selection_dates = sorted({windows[index][0] for index in range(N_WINDOWS)})
    industry_map = _industry_map(INDUSTRY_PATH, selection_dates)
    _stage('s5 industry')

    runtime_store = PointInTimeMarketStore(RUNTIME_PIT_ROOT)
    benchmark_bundles = {}
    for benchmark in BENCHMARKS:
        bundle = runtime_store.read(benchmark)
        if bundle is not None:
            benchmark_bundles[benchmark] = bundle
    benchmark_precomp = {
        benchmark: {
            'dates': pd.DatetimeIndex(
                pd.to_datetime(bundle.prices['date'], errors='coerce')
            ),
            'qfq': pd.to_numeric(
                bundle.prices['qfq_close'], errors='coerce'
            ).to_numpy(),
            'raw': pd.to_numeric(
                bundle.prices['raw_close'], errors='coerce'
            ).to_numpy(),
        }
        for benchmark, bundle in benchmark_bundles.items()
    }
    _stage('s6 benchmarks')

    window_rows = []
    window_top20 = {}
    for wi in range(N_WINDOWS):
        _stage('w%d start' % wi)
        ws_date, we_date = windows[wi]
        prior = [index for index in range(wi) if windows[index][1] <= ws_date]
        if len(prior) < min_windows:
            continue
        momentum_pct = {}
        vol_pct = {}
        for code in universe:
            series = returns_by_code[code][prior]
            series = series[np.isfinite(series)]
            if len(series) < min_windows:
                continue
            median_return = float(np.median(series))
            annualized = (1.0 + median_return) ** (12.0 / TEST_MONTHS) - 1.0
            momentum_pct[code] = annualized * MOMENTUM_HALF * 100.0
            vol_pct[code] = float(
                np.std(series, ddof=1) * np.sqrt(12.0 / TEST_MONTHS) * 100.0
            )
        fundamentals = {}
        pb_at_selection = {}
        for code in universe:
            stats = _fundamentals_as_of(annual_cache[code], ws_date)
            if stats is None:
                continue
            nroe, g_fund, book_per_share = stats
            if book_per_share is None or book_per_share <= 0:
                continue
            dates = precomp[code]['dates']
            raw = precomp[code]['raw']
            index = int(dates.searchsorted(pd.Timestamp(ws_date), side='right')) - 1
            if index < 0 or not np.isfinite(raw[index]) or raw[index] <= 0:
                continue
            fundamentals[code] = (nroe, g_fund, book_per_share)
            pb_at_selection[code] = float(raw[index] / book_per_share)
        momentum_map = {
            code: {'momentum_pct': momentum_pct.get(code),
                   'vol_pct': vol_pct.get(code)}
            for code in universe
        }
        ke10_all, candidates = _select_window(
            momentum_map, fundamentals, pb_at_selection,
            vol_pct, universe, industry_map.get(ws_date, {})
        )
        if len(candidates) < 1:
            continue
        _stage('w%d steps: pre=%d' % (wi, len(candidates)))
        candidates.sort(key=lambda item: -(item.get('vol_pct') or 0.0))
        trim = min(VOL_EXCLUDE_TOP, max(0, len(candidates) - top_n))
        candidates = candidates[trim:]
        _stage('w%d after vol=%d' % (wi, len(candidates)))
        before = len(candidates)
        candidates = [item for item in candidates if item['momentum_pct'] is not None
                      and item['momentum_pct'] >= MOMENTUM_MIN_PCT]
        _stage('w%d after mom>=%.0f%%=%d (pre %d)' % (
            wi, MOMENTUM_MIN_PCT, len(candidates), before))
        candidates.sort(key=lambda item: (-item['momentum_pct'], item['code']))
        trim = min(MOM_EXCLUDE_TOP, max(0, len(candidates) - top_n))
        candidates = candidates[trim:]
        _stage('w%d after momtop=%d' % (wi, len(candidates)))
        candidates.sort(key=lambda item: (item['pb'], item['code']))
        trim = min(PB_LOW_TOP, max(0, len(candidates) - top_n))
        candidates = candidates[trim:]
        _stage('w%d after pblow=%d' % (wi, len(candidates)))
        candidates.sort(key=lambda item: (-item['discount10'], item['code']))
        top = candidates[:top_n]
        top_codes = [item['code'] for item in top]
        assert all(
            item['discount10'] >= 1.0 - 1e-9 for item in candidates
        ), 'discount<1 survivors after Ke10 screen'
        _stage('w%d selected: ke10=%d survivors=%d top=%d' % (
            wi, len(ke10_all), len(candidates), len(top)))

        vol_codes = sorted(vol_pct, key=lambda code: (vol_pct[code], code))[:top_n]
        grid = common[(common >= pd.Timestamp(ws_date))
                    & (common <= pd.Timestamp(we_date))]
        if len(grid) < 5:
            continue
        series_map = {}
        for code in top_codes + ke10_all + vol_codes:
            if code not in series_map:
                series_map[code] = _series_on_grid(code, bundles[code], grid, precomp)
        model = {
            code: (fundamentals[code][1], fundamentals[code][0],
                   fundamentals[code][2])
            for code in top_codes if code in fundamentals
        }
        basket, trades = _basket_nav(top_codes, series_map, grid, model)
        stats = _compute_stats(basket)
        universe_series = {
            code: series_map.get(code) or _series_on_grid(code, bundles[code], grid, precomp)
            for code in universe
        }
        naives = {}
        naives['universe_ew'] = _compute_stats(_hold_nav(universe, universe_series, grid))
        roepb_series = {
            code: universe_series[code] for code in ke10_all if code in universe_series
        }
        naives['roepb_ew'] = _compute_stats(_hold_nav(ke10_all, roepb_series, grid))
        naives['lowvol_ew'] = _compute_stats(_hold_nav(vol_codes, series_map, grid))
        benchmark_series = {}
        for benchmark in BENCHMARKS:
            bundle = benchmark_bundles.get(benchmark)
            if bundle is None:
                continue
            series = _series_on_grid(benchmark, bundle, grid, benchmark_precomp)
            if series is None:
                continue
            benchmark_series[benchmark] = _compute_stats(
                _hold_nav([benchmark], {benchmark: series}, grid)
            )
        window_rows.append({
            'window_index': wi,
            'selection_date': str(ws_date),
            'test_start': str(grid[0].date()),
            'test_end': str(grid[-1].date()),
            'ke10_count': len(ke10_all),
            'survivors_count': len(candidates),
            'top20_codes': top_codes,
            'strategy': stats,
            'sells': trades,
            'naive': naives,
            'benchmarks': benchmark_series,
        })
        window_top20[wi] = top
        _stage('w%d evaluated' % wi)

    _print_report(window_rows, window_top20)
    output_dir = args.output_root / datetime.now(timezone.utc).astimezone().strftime(
        '%Y%m%d_%H%M%S'
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'report.json').write_text(
        json.dumps({
            'created_at': datetime.now(timezone.utc).isoformat(),
            'model': {'ke_buy': KE_BUY, 'ke_sell': KE_SELL,
                      'roe_norm_years': ROE_NORM_YEARS,
                      'growth_years': GROWTH_YEARS,
                      'growth_cap': GROWTH_CAP, 'growth_floor': GROWTH_FLOOR,
                      'growth_fallback': GROWTH_FALLBACK,
                      'momentum_half': MOMENTUM_HALF,
                      'momentum_min_pct': MOMENTUM_MIN_PCT,
                      'vol_exclude_top': VOL_EXCLUDE_TOP,
                      'mom_exclude_top': MOM_EXCLUDE_TOP,
                      'pb_low_top': PB_LOW_TOP,
                      'excluded_industries': sorted(EXCLUDED_INDUSTRIES)},
            'windows': window_rows,
        }, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print('artifacts saved under: %s' % output_dir, flush=True)
    return 0


def _print_report(window_rows, window_top20):
    print()
    print('=' * 116)
    print('价值-动量复合 v3 | Ke_implied = g_fund + (nROE - g_fund) / PB')
    print('买入 Ke_implied >= 10%  |  卖出 Ke_implied < 6%')
    print('nROE = 近5年年度ROE中位 | g_fund = 5年账面增长(0~6%%); momentum = 窗口收益中位年化/2')
    print('剔除: 波动TOP20 + momentum<2%% + momentum TOP20 + PB最低TOP20 + 银行/证券/保险')
    print('=' * 116)
    print('%-5s %-11s %-11s %8s %7s %7s %8s %7s %8s %7s %8s %8s %6s' % (
        'win', 'sel', 'test_end', 'strat%', 'dd%', 'sharpe', 'univ%', 'univdd%', 'roepb%', 'lowvol%', 'csi300%', 'csi500%', 'sells'
    ))
    for row in window_rows:
        st = row['strategy']
        u = (row['naive'] or {}).get('universe_ew', {})
        rp = (row['naive'] or {}).get('roepb_ew', {})
        lv = (row['naive'] or {}).get('lowvol_ew', {})
        b300 = (row['benchmarks'] or {}).get('510300', {})
        b500 = (row['benchmarks'] or {}).get('510500', {})
        print('%-5d %-11s %-11s %8.2f %7.2f %7.3f %8.2f %7.2f %8.2f %7.2f %8.2f %8.2f %6d' % (
            row['window_index'], row['selection_date'], row['test_end'],
            st.get('return_pct', 0.0), st.get('max_drawdown_pct', 0.0),
            st.get('sharpe_ratio', 0.0),
            u.get('return_pct', 0.0), u.get('max_drawdown_pct', 0.0),
            rp.get('return_pct', 0.0), lv.get('return_pct', 0.0),
            b300.get('return_pct', 0.0), b500.get('return_pct', 0.0),
            row.get('sells', 0),
        ))
    if window_rows:
        st_returns = [row['strategy']['return_pct'] for row in window_rows]
        st_dd = [row['strategy']['max_drawdown_pct'] for row in window_rows]
        st_sh = [row['strategy']['sharpe_ratio'] for row in window_rows]
        print()
        print('strategy: %d windows | mean %.2f%% | median %.2f%% | worstDD %.2f%% | meanSharpe %.3f' % (
            len(window_rows), float(np.mean(st_returns)), float(np.median(st_returns)),
            float(min(st_dd)), float(np.mean(st_sh))
        ))
        for label, source in (('510300', 'benchmarks'), ('510500', 'benchmarks'),
                              ('universe_ew', 'naive'), ('roepb_ew', 'naive'),
                              ('lowvol_ew', 'naive')):
            wins = 0
            for row in window_rows:
                bucket = row.get(source, {})
                benchmark = bucket.get(label, {})
                target = float(benchmark.get('return_pct', -1e9))
                if row['strategy']['return_pct'] > target:
                    wins += 1
            print('  wins vs %s: %d / %d' % (label, wins, len(window_rows)))
    for wi, top in window_top20.items():
        print()
        print('window %d top20 (disc10): %s' % (wi,
              ','.join(item['code'] + ':' + ('%.2f' % item['discount10'])
                       for item in top)))


if __name__ == "__main__":
    raise SystemExit(main())