#!/usr/bin/env python3
'''Two cheap falsification experiments for the frozen v3 screen.

1. Tail-filter ablation on two representative windows (weak market w11,
   bull market w17).  Variants:  A = v3 (vol-excl + mom>=2% + mom-top-20 +
   PB-low-20);  B = A without the hottest-momentum exclusion;  C = A without
   the highest-volatility exclusion;  D = A without both tail filters.
2. (m/2) calibration: does the momentum band-pass lower-bound predict future
   per-share fundamental growth?  Formation cohorts are built from the same
   PIT snapshot; g_actual is the forward BVPS CAGR over the data we have.
'''

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.market_history import PointInTimeMarketStore
from src.instruments.point_in_time import PointInTimeFundamentalStore


def _load_v3():
    spec = importlib.util.spec_from_file_location(
        'v3', ROOT / 'scripts' / 'screen_csi300_500_justified_pb_v3.py'
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules['v3'] = module
    spec.loader.exec_module(module)
    return module


v3 = _load_v3()


def _apply_filters(candidates, top_n, use_vol=True, use_momentum_top=True):
    '''v3 filter chain with the two tail filters switchable.'''
    pool = list(candidates)
    if use_vol:
        pool.sort(key=lambda item: -(item.get('vol_pct') or 0.0))
        trim = min(v3.VOL_EXCLUDE_TOP, max(0, len(pool) - top_n))
        pool = pool[trim:]
    pool = [
        item for item in pool
        if item['momentum_pct'] is not None
        and item['momentum_pct'] >= v3.MOMENTUM_MIN_PCT
    ]
    if use_momentum_top:
        pool.sort(key=lambda item: (-item['momentum_pct'], item['code']))
        trim = min(v3.MOM_EXCLUDE_TOP, max(0, len(pool) - top_n))
        pool = pool[trim:]
    pool.sort(key=lambda item: (item['pb'], item['code']))
    trim = min(v3.PB_LOW_TOP, max(0, len(pool) - top_n))
    pool = pool[trim:]
    pool.sort(key=lambda item: (-item['discount10'], item['code']))
    return pool[:top_n]


def _window_selection(state, wi, top_n):
    '''Rebuild the v3 formation snapshot for one window (PIT only).'''
    windows = state['windows']
    ws_date, we_date = windows[wi]
    prior = [index for index in range(wi) if windows[index][1] <= ws_date]
    if len(prior) < v3.MIN_MOMENTUM_WINDOWS:
        return None
    momentum_pct = {}
    vol_pct = {}
    for code in state['universe']:
        series = state['returns_by_code'][code][prior]
        series = series[np.isfinite(series)]
        if len(series) < v3.MIN_MOMENTUM_WINDOWS:
            continue
        median_return = float(np.median(series))
        annualized = (1.0 + median_return) ** (12.0 / v3.TEST_MONTHS) - 1.0
        momentum_pct[code] = annualized * v3.MOMENTUM_HALF * 100.0
        vol_pct[code] = float(
            np.std(series, ddof=1) * np.sqrt(12.0 / v3.TEST_MONTHS) * 100.0
        )
    fundamentals = {}
    pb_at_selection = {}
    for code in state['universe']:
        stats = v3._fundamentals_as_of(state['annual_cache'][code], ws_date)
        if stats is None:
            continue
        nroe, g_fund, book_per_share = stats
        if book_per_share is None or book_per_share <= 0:
            continue
        dates = state['precomp'][code]['dates']
        raw = state['precomp'][code]['raw']
        index = int(dates.searchsorted(pd.Timestamp(ws_date), side='right')) - 1
        if index < 0 or not np.isfinite(raw[index]) or raw[index] <= 0:
            continue
        fundamentals[code] = (nroe, g_fund, book_per_share)
        pb_at_selection[code] = float(raw[index] / book_per_share)
    momentum_map = {
        code: {'momentum_pct': momentum_pct.get(code),
               'vol_pct': vol_pct.get(code)}
        for code in state['universe']
    }
    ke10_all, candidates = v3._select_window(
        momentum_map, fundamentals, pb_at_selection,
        vol_pct, state['universe'], state['industry_map'].get(ws_date, {})
    )
    return {
        'ws_date': ws_date,
        'we_date': we_date,
        'ke10_all': ke10_all,
        'candidates': candidates,
        'fundamentals': fundamentals,
    }


def _evaluate_basket(state, snapshot, codes, top_n):
    windows = state['windows']
    ws_date, we_date = snapshot['ws_date'], snapshot['we_date']
    grid = state['common'][(state['common'] >= pd.Timestamp(ws_date))
                           & (state['common'] <= pd.Timestamp(we_date))]
    series_map = {}
    for code in codes:
        series_map[code] = v3._series_on_grid(
            code, state['bundles'][code], grid, state['precomp']
        )
    model = {
        code: (snapshot['fundamentals'][code][1], snapshot['fundamentals'][code][0],
               snapshot['fundamentals'][code][2])
        for code in codes if code in snapshot['fundamentals']
    }
    nav, sells = v3._basket_nav(codes, series_map, grid, model)
    stats = v3._compute_stats(nav)
    return stats, sells, len(codes)


def _bucket_label(value):
    if value < 2.0:
        return '0-2'
    if value < 4.0:
        return '2-4'
    if value < 6.0:
        return '4-6'
    if value < 8.0:
        return '6-8'
    return '>8'


def _calibration(state, min_horizon=1.0, max_formation_index=None):
    '''Cohort study: does m/2 predict forward BVPS CAGR?'''
    rows = []
    windows = state['windows']
    end_date = state['market_end']
    for wi in range(v3.N_WINDOWS):
        if max_formation_index is not None and wi > max_formation_index:
            continue
        ws_date, _we_date = windows[wi]
        prior = [index for index in range(wi) if windows[index][1] <= ws_date]
        if len(prior) < v3.MIN_MOMENTUM_WINDOWS:
            continue
        for code in state['universe']:
            series = state['returns_by_code'][code][prior]
            series = series[np.isfinite(series)]
            if len(series) < v3.MIN_MOMENTUM_WINDOWS:
                continue
            median_return = float(np.median(series))
            annualized = (1.0 + median_return) ** (12.0 / v3.TEST_MONTHS) - 1.0
            g_hat = annualized * v3.MOMENTUM_HALF
            annuals = state['annual_cache'][code]
            g_actual, years = v3._bvps_cagr(annuals, ws_date, end_date)
            if g_actual is None or years is None or years < min_horizon:
                continue
            rows.append({
                'formation': str(ws_date),
                'window_index': wi,
                'code': code,
                'g_hat_pct': g_hat * 100.0,
                'g_actual_pct': g_actual * 100.0,
                'horizon_years': years,
                'bucket': _bucket_label(g_hat * 100.0),
            })
    return rows


def _summarize_calibration(rows):
    order = ['0-2', '2-4', '4-6', '6-8', '>8']
    summary = {}
    for bucket in order:
        items = [row for row in rows if row['bucket'] == bucket]
        if not items:
            summary[bucket] = {'n': 0}
            continue
        g_hat = np.asarray([row['g_hat_pct'] for row in items])
        g_actual = np.asarray([row['g_actual_pct'] for row in items])
        summary[bucket] = {
            'n': len(items),
            'g_hat_median_pct': round(float(np.median(g_hat)), 2),
            'g_actual_mean_pct': round(float(np.mean(g_actual)), 2),
            'g_actual_median_pct': round(float(np.median(g_actual)), 2),
            'g_actual_p25_pct': round(float(np.percentile(g_actual, 25)), 2),
            'g_actual_p75_pct': round(float(np.percentile(g_actual, 75)), 2),
            'prob_ghat_gt_actual': round(float(np.mean(g_hat > g_actual)), 3),
            'prob_ghat_minus_3pp': round(
                float(np.mean(g_hat - g_actual > 3.0)), 3
            ),
            'corr_ghat_actual': round(
                float(np.corrcoef(g_hat, g_actual)[0, 1])
                if len(items) > 2 else 0.0, 3
            ),
        }
    horizons = [row['horizon_years'] for row in rows]
    summary['_meta'] = {
        'rows': len(rows),
        'min_horizon': min(horizons) if horizons else None,
        'max_horizon': max(horizons) if horizons else None,
    }
    return summary


def _build_state():
    _, pool, statements = v3._load_pool()
    store = PointInTimeMarketStore(v3.DATASET_ROOT)
    codes = sorted(set(pool) & statements)
    bundles = {}
    for code in codes:
        bundle = store.read(code)
        if bundle is not None:
            bundles[code] = bundle
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
    common = max(mainstream, key=lambda item: item[0])[1].sort_values()
    windows, _horizon_start, market_end = v3._window_geometry(common)
    first_start, last_end = windows[0][0], windows[-1][1]
    universe = [
        code for code, bundle in bundles.items()
        if str(pd.Timestamp(bundle.prices['date'].iloc[0]).date()) <= str(first_start)
        and str(pd.Timestamp(bundle.prices['date'].iloc[-1]).date()) >= str(last_end)
    ]
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
    returns_by_code = {}
    for code in universe:
        dates = precomp[code]['dates']
        qfq = precomp[code]['qfq']
        values = []
        for start, end in windows:
            value = v3._window_return(dates, qfq, start, end)
            values.append(value if value is not None else np.nan)
        returns_by_code[code] = np.asarray(values, dtype=np.float64)
    fundamental_store = PointInTimeFundamentalStore(v3.DATASET_ROOT)
    annual_cache = {
        code: v3._annual_statement_series(fundamental_store, code)
        for code in universe
    }
    selection_dates = sorted({windows[index][0] for index in range(v3.N_WINDOWS)})
    industry_map = v3._industry_map(v3.INDUSTRY_PATH, selection_dates)
    return {
        'pool': pool,
        'bundles': bundles,
        'common': common,
        'windows': windows,
        'market_end': market_end,
        'universe': universe,
        'precomp': precomp,
        'returns_by_code': returns_by_code,
        'annual_cache': annual_cache,
        'industry_map': industry_map,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows', type=str, default='11,17')
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--skip-calibration', action='store_true')
    parser.add_argument('--output-root', type=Path, default=
        ROOT / 'data' / 'analysis' / 'justified_pb_ablation'
    )
    args = parser.parse_args()
    window_indexes = [int(item) for item in args.windows.split(',') if item.strip()]
    print('building state ...', flush=True)
    state = _build_state()
    print('universe=%d windows=%d' % (len(state['universe']), len(state['windows'])),
          flush=True)

    variants = [
        ('A_v3_full', True, True),
        ('B_keep_hottest_momentum', True, False),
        ('C_keep_highest_vol', False, True),
        ('D_keep_both_tails', False, False),
    ]
    ablation = {}
    for wi in window_indexes:
        snapshot = _window_selection(state, wi, args.top_n)
        if snapshot is None:
            print('window %d has no snapshot' % wi, flush=True)
            continue
        entry = {
            'selection_date': str(snapshot['ws_date']),
            'test_start': str(snapshot['ws_date']),
            'test_end': str(snapshot['we_date']),
            'ke10_count': len(snapshot['ke10_all']),
            'variants': {},
        }
        for name, use_vol, use_momentum_top in variants:
            basket = _apply_filters(
                snapshot['candidates'], args.top_n, use_vol, use_momentum_top
            )
            codes = [item['code'] for item in basket]
            stats, sells, size = _evaluate_basket(state, snapshot, codes, args.top_n)
            entry['variants'][name] = {
                'size': size,
                'return_pct': stats['return_pct'],
                'max_drawdown_pct': stats['max_drawdown_pct'],
                'sharpe_ratio': stats['sharpe_ratio'],
                'sells': sells,
                'codes': codes,
            }
        benchmark_row = _window_benchmarks(state, snapshot)
        entry['benchmarks'] = benchmark_row
        ablation['w%d' % wi] = entry
        print()
        print('window %d | sel %s | test %s -> %s | ke10=%d' % (
            wi, entry['selection_date'], entry['test_start'], entry['test_end'],
            entry['ke10_count']
        ), flush=True)
        for name, _uv, _um in variants:
            payload = entry['variants'][name]
            print('  %-26s n=%-2d ret=%+7.2f%% dd=%7.2f%% sharpe=%6.3f' % (
                name, payload['size'], payload['return_pct'],
                payload['max_drawdown_pct'], payload['sharpe_ratio']
            ), flush=True)
        print('  benchmarks: %s' % json.dumps(benchmark_row, ensure_ascii=False), flush=True)

    calibration = None
    if not args.skip_calibration:
        print()
        print('calibration cohort ...', flush=True)
        rows = _calibration(state)
        calibration = {'summary': _summarize_calibration(rows), 'rows': rows}
        summary = calibration['summary']
        print('  m/2 bucket |   n | g_hat med | g_actual mean | med | p25 | p75 | P(ghat>act) | P(diff>3pp) | corr' , flush=True)
        for bucket in ['0-2', '2-4', '4-6', '6-8', '>8']:
            item = summary[bucket]
            if item.get('n'):
                print('  %-10s | %3d | %9.2f | %13.2f | %5.2f | %5.2f | %5.2f | %11.3f | %10.3f | %5.3f' % (
                    bucket, item['n'], item['g_hat_median_pct'],
                    item['g_actual_mean_pct'], item['g_actual_median_pct'],
                    item['g_actual_p25_pct'], item['g_actual_p75_pct'],
                    item['prob_ghat_gt_actual'], item['prob_ghat_minus_3pp'],
                    item['corr_ghat_actual']
                ), flush=True)
            else:
                print('  %-10s |   0' % bucket, flush=True)
        print('  horizon years: %s' % json.dumps(summary['_meta']), flush=True)

    output_dir = args.output_root / datetime.now(timezone.utc).astimezone().strftime(
        '%Y%m%d_%H%M%S'
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'report.json').write_text(
        json.dumps({'ablation': ablation, 'calibration': calibration},
                   ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print('artifacts saved under: %s' % output_dir, flush=True)
    return 0


def _window_benchmarks(state, snapshot):
    ws_date, we_date = snapshot['ws_date'], snapshot['we_date']
    grid = state['common'][(state['common'] >= pd.Timestamp(ws_date))
                           & (state['common'] <= pd.Timestamp(we_date))]
    universe_series = {}
    for code in state['universe']:
        universe_series[code] = v3._series_on_grid(
            code, state['bundles'][code], grid, state['precomp']
        )
    result = {}
    result['universe_ew'] = v3._compute_stats(
        v3._hold_nav(state['universe'], universe_series, grid)
    )['return_pct']
    result['roepb_ew'] = v3._compute_stats(
        v3._hold_nav(snapshot['ke10_all'], universe_series, grid)
    )['return_pct']
    runtime = PointInTimeMarketStore(v3.RUNTIME_PIT_ROOT)
    for benchmark in v3.BENCHMARKS:
        bundle = runtime.read(benchmark)
        if bundle is None:
            continue
        precomp = {
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
        }
        series = v3._series_on_grid(benchmark, bundle, grid, precomp)
        if series is None:
            continue
        result[benchmark] = v3._compute_stats(
            v3._hold_nav([benchmark], {benchmark: series}, grid)
        )['return_pct']
    return result


if __name__ == '__main__':
    raise SystemExit(main())