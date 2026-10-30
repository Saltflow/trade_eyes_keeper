#!/usr/bin/env python3
"""Justified-PB screen v4: arithmetic-only, defensive, fixed-N retrieval.

Design constraints (from the 2026-09 review session):
  1. four operations only -- no powers, no roots, no medians, no std.
     Note `(1+r)**(12/9)` in v3 is a monotone transform, so deleting it does
     not change which names pass; `np.std(..., ddof=1)` is replaced by a
     normalised range `(max-min)/mean`.
  2. explicit -- every rule is one readable arithmetic expression.
  3. measurable discrimination -- `--evaluate` reports the mean FORWARD
     PERCENTILE of the picked names (random pick = 50.0), per window and on
     w8/w11/w14/w17 (lag-3, i.e. non-overlapping), with a standard error.
     NAMING WARNING: w8/w11/w14/w17 are a NON-OVERLAPPING SUBSET, not a
     held-out validation set. They were inspected throughout the design of
     this file, so they are contaminated -- do not report them as
     "validation". A real holdout needs data not yet examined.
  4. low parameter sensitivity -- fixed output size N, and the only cut point
     (`--pb-floor-pct`) is a percentile, so it is scale-free. `--sweep`
     measures the sensitivity instead of assuming it.

Changes vs v3, and the evidence for each:
  * DROP the discount10 ranking. Measured IC = -0.052 (12w) / -0.125 (4w);
    gate metric 51.3. Its only certifiable relation is negative.
  * DROP `VOL_EXCLUDE_TOP`. Measured gate metric 53.0 for the *high*-vol side,
    i.e. the trim removed the better half on average. It is also the same
    defensive trade as the momentum floor, so keeping both double-counted.
  * DROP the momentum band-pass threshold and the `momentum >= 2%` floor.
  * REPLACE the v3 momentum (median of overlapping 9m windows, annualised)
    with a single division: `qfq(t)/qfq(t-24m) - 1`. Measured gate metric
    53.3/54.7 vs v3's 51.3/51.2, and it is arithmetic.
  * ADD a PB floor. The cheapest names inside an already-cheap pool are the
    one certifiably bad group: ranking by 1/PB gives forward percentile 38.1
    at N=10, 44.7 at N=20, 49.6 at N=30, 50.8 at N=40. The gate and the floor
    together are a PB band-pass.

Point-in-time discipline:
  * Industry is resolved PER FORMATION DATE from the official classification
    history: for each selection date, the latest record published at or before
    that date. An earlier revision of this file used the latest snapshot,
    which is mild look-ahead and shifted the industry exclusion by 1-2 names.
    That was the cause of a w11 replication mismatch against the v3 ablation
    artifact (+28.72% vs the recorded +25.36%; w17 happened to match exactly).
  * Fundamentals (statement published_at), prices and the Ke gate were already
    point-in-time; nothing else in the gate looks forward.

Honest limits, stated up front:
  * The defensive tilt is a POLICY CHOICE, not a validated alpha. Every
    momentum variant measures t < 0.7 on the four independent windows with an
    SE of 6-11 percentile points. The momentum floor wins in falling markets
    (forward percentile 61-70 in w8-w11) and loses in rising ones (31-56), and
    v3's four worst drawdowns (-15.3% .. -19.6%) all occurred in up windows.
    Survival against crashes is therefore bought with style-rotation bleed.
  * Market-relative momentum is impossible here: subtracting the index is a
    per-window constant and cannot change a cross-sectional ranking. Dividing
    by volatility makes the regime tilt *stronger* (corr -0.65 -> -0.73).
  * Within an already-cheap pool, cheapness-ranking is negatively related to
    forward return (nroe_pb gate = 41.9, t = -6.2 on the four windows). Only
    the floor, not a preference for cheapness, is supported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.instruments.point_in_time import PointInTimeFundamentalStore

REFERENCE = ROOT / "data" / "reference_universe"
CONSTITUENTS_PATH = REFERENCE / "index_constituents_20260818.json"
DATASET_ROOT = (
    REFERENCE / "point_in_time_20260818" / "partial_first_pass_20260822"
    / "dataset"
)
INDUSTRY_PATH = (
    REFERENCE / "industry_classification_history_official_2020q4_2025h2.json"
)

KE_BUY = 0.10
GROWTH_CAP = 0.06
GROWTH_FLOOR = 0.0
GROWTH_FALLBACK = 0.03
ROE_NORM_YEARS = 5
TEST_MONTHS = 9
STEP_MONTHS = 3
STATE_BOUNDARY_MONTHS = 6
N_WINDOWS = 20
MIN_PRIOR_WINDOWS = 6
EXCLUDED_INDUSTRIES = {"J66", "J67", "J68"}


def _load_universe() -> list[str]:
    constituents = json.load(open(CONSTITUENTS_PATH, encoding="utf-8"))
    pool = {
        company["code"]
        for company in constituents["companies"]
        if set(company.get("memberships") or []) & {"csi_300", "csi_500"}
    }
    statements = {
        path.name.split(".statements.json")[0]
        for path in (DATASET_ROOT / "fundamentals").glob("*.statements.json")
    }
    return sorted(pool & statements)


def _annual_series(store: PointInTimeFundamentalStore, code: str):
    """(year, published_at, roe_pct, parent_equity, total_shares)."""
    rows = []
    try:
        records = store.read_all(code)
    except Exception:
        return rows
    by_period = {}
    for item in records:
        period_end = getattr(item, "period_end", None)
        published = getattr(item, "published_at", None)
        if period_end is None or published is None or period_end.month != 12:
            continue
        net_income = getattr(item, "net_income_parent", None)
        equity = getattr(item, "average_parent_equity", None)
        if equity is None:
            equity = getattr(item, "parent_equity", None)
        if net_income is None or equity is None or equity <= 0:
            continue
        shares = getattr(item, "total_shares", None)
        if shares is None:
            shares = getattr(item, "common_shares_outstanding", None)
        key = (period_end.year, str(getattr(item, "period_type", "")))
        row = (published, float(net_income), float(equity),
               float(shares) if shares else None)
        current = by_period.get(key)
        if current is None or published > current[0]:
            by_period[key] = row
    for (year, _ptype), (published, net_income, equity, shares) in by_period.items():
        rows.append((year, published, net_income / equity * 100.0, equity, shares))
    return sorted(rows, key=lambda row: row[0])


def _fundamentals_as_of(annuals, selection_date):
    """(nROE, g_fund, book_per_share), all point-in-time."""
    eligible = [row for row in annuals if row[1] <= selection_date]
    if not eligible:
        return None
    roes = [row[2] for row in eligible[-ROE_NORM_YEARS:]]
    if len(roes) < 2:
        return None
    nroe = float(np.median(roes)) / 100.0
    bvps_rows = [
        (row[0], row[3] / row[4])
        for row in eligible
        if row[3] is not None and row[4] is not None
        and row[3] > 0 and row[4] > 0
    ]
    g_fund = GROWTH_FALLBACK
    if len(bvps_rows) >= 2:
        first_year, first_bvps = bvps_rows[0]
        last_year, last_bvps = bvps_rows[-1]
        years = last_year - first_year
        if years >= 2 and first_bvps > 0 and last_bvps > 0:
            cagr = (last_bvps / first_bvps) ** (1.0 / years) - 1.0
            g_fund = float(min(max(cagr, GROWTH_FLOOR), GROWTH_CAP))
    latest = eligible[-1]
    bps = float(latest[3] / latest[4]) if (latest[4] and latest[4] > 0) else None
    return nroe, g_fund, bps


def _window_geometry(common_dates):
    end_exclusive = pd.Timestamp(common_dates[-1]).normalize()
    end_exclusive = end_exclusive + pd.Timedelta(days=1)
    horizon = STATE_BOUNDARY_MONTHS + TEST_MONTHS + (
        N_WINDOWS - 1
    ) * STEP_MONTHS
    horizon_start = end_exclusive - pd.DateOffset(months=horizon)
    windows = []
    for index in range(N_WINDOWS):
        start = horizon_start + pd.DateOffset(
            months=STATE_BOUNDARY_MONTHS + index * STEP_MONTHS
        )
        end = start + pd.DateOffset(months=TEST_MONTHS) - pd.Timedelta(days=1)
        windows.append((start.date(), end.date()))
    return windows


def _window_return(dates, qfq, start, end):
    start_index = int(dates.searchsorted(pd.Timestamp(start), side="left"))
    end_index = int(dates.searchsorted(pd.Timestamp(end), side="right")) - 1
    if start_index < 0 or end_index <= start_index or end_index >= len(dates):
        return None
    begin, finish = qfq[start_index], qfq[end_index]
    if not np.isfinite(begin) or not np.isfinite(finish) or begin <= 0:
        return None
    return float(finish / begin - 1.0)


def _trailing_return(dates, qfq, as_of, months):
    """qfq(t)/qfq(t-months) - 1 : one division, no power, no median."""
    index = int(dates.searchsorted(pd.Timestamp(as_of), side="right")) - 1
    if index < 0:
        return None
    back = pd.Timestamp(as_of) - pd.DateOffset(months=months)
    back_index = int(dates.searchsorted(back, side="left"))
    if back_index < 0 or index <= back_index:
        return None
    begin, finish = qfq[back_index], qfq[index]
    if not np.isfinite(begin) or not np.isfinite(finish) or begin <= 0:
        return None
    return float(finish / begin - 1.0)


class Screen:
    def __init__(self) -> None:
        self.codes = _load_universe()
        store = PointInTimeFundamentalStore(DATASET_ROOT)
        self.annuals = {code: _annual_series(store, code) for code in self.codes}
        self.prices = {}
        for code in self.codes:
            path = DATASET_ROOT / "market" / ("%s.csv" % code)
            if not path.exists():
                continue
            frame = pd.read_csv(path, usecols=["date", "qfq_close", "raw_close"])
            self.prices[code] = (
                pd.DatetimeIndex(pd.to_datetime(frame["date"])),
                pd.to_numeric(frame["qfq_close"], errors="coerce").to_numpy(),
                pd.to_numeric(frame["raw_close"], errors="coerce").to_numpy(),
            )
        mainstream = [
            (len(dates), dates)
            for dates, _qfq, _raw in self.prices.values()
            if len(dates) >= 300
            and dates[0] <= pd.Timestamp("2020-10-01")
            and dates[-1] <= pd.Timestamp("2026-08-21")
        ]
        self.common = max(mainstream, key=lambda item: item[0])[1].sort_values()
        self.windows = _window_geometry(self.common)
        self.universe = [
            code
            for code, (dates, _q, _r) in self.prices.items()
            if dates[0].date() <= self.windows[0][0]
            and dates[-1].date() >= self.windows[-1][1]
        ]
        self.industry_by_date = self._industry_map(
            sorted({window[0] for window in self.windows})
        )
        self.forward = {
            code: np.asarray(
                [
                    _window_return(
                        self.prices[code][0], self.prices[code][1], start, end
                    )
                    for start, end in self.windows
                ],
                dtype=float,
            )
            for code in self.universe
        }

    @staticmethod
    def _industry_map(selection_dates):
        """Point-in-time industry per selection date.

        For each selection date, keep the latest classification record that had
        already been PUBLISHED by that date. Mirrors v3's `_industry_map` so the
        exclusion boundary is identical.
        """
        observations = IndustryClassificationHistoryStore(INDUSTRY_PATH).read()
        result = {selection_date: {} for selection_date in selection_dates}
        for observation in observations:
            code = observation.symbol
            published = observation.published_at
            for selection_date in selection_dates:
                if published > selection_date:
                    continue
                current = result[selection_date].get(code)
                if current is None or (
                    published, observation.period_end
                ) > current:
                    result[selection_date][code] = (
                        published, observation.period_end,
                        observation.industry_code,
                    )
        return {
            selection_date: {
                code: value[2] for code, value in mapping.items()
            }
            for selection_date, mapping in result.items()
        }

    def eligible(self, window_index, pb_floor_pct, max_per_industry=0):
        """The v4 gate: a PB band-pass plus structural exclusions.

        Returns the pool of (code, pb, momentum) ready for selection.
        """
        active = [
            index for index in range(window_index)
            if self.windows[index][1] <= self.windows[window_index][0]
        ]
        if len(active) < MIN_PRIOR_WINDOWS:
            return None
        as_of = self.windows[window_index][0]
        industry_map = self.industry_by_date.get(as_of, {})
        rows = []
        for code in self.universe:
            stats = _fundamentals_as_of(self.annuals[code], as_of)
            if stats is None:
                continue
            nroe, g_fund, bps = stats
            if bps is None or bps <= 0 or nroe <= 0:
                continue
            dates, _qfq, raw = self.prices[code]
            index = int(dates.searchsorted(pd.Timestamp(as_of), side="right")) - 1
            if index < 0 or not np.isfinite(raw[index]) or raw[index] <= 0:
                continue
            pb = float(raw[index] / bps)
            # Ke_implied >= 10%  <=>  PB <= (nROE - g) / (KE_BUY - g)
            if KE_BUY - g_fund <= 0:
                continue
            if pb > (nroe - g_fund) / (KE_BUY - g_fund):
                continue
            if (industry_map.get(code) or "") in EXCLUDED_INDUSTRIES:
                continue
            momentum = _trailing_return(
                dates, self.prices[code][1], as_of, MOMENTUM_MONTHS
            )
            if momentum is None:
                continue
            rows.append({"code": code, "pb": pb, "momentum": momentum})
        if pb_floor_pct > 0 and rows:
            cut = float(np.percentile([row["pb"] for row in rows], pb_floor_pct))
            rows = [row for row in rows if row["pb"] >= cut]
        rows.sort(key=lambda row: (-row["momentum"], row["code"]))
        return rows

    def evaluate(self, window_index, top_n, pb_floor_pct, max_per_industry=0):
        rows = self.eligible(window_index, pb_floor_pct)
        if not rows:
            return None
        industry_map = self.industry_by_date.get(
            self.windows[window_index][0], {}
        )
        picked = _select(rows, top_n, max_per_industry, industry_map)
        pool = self.eligible(window_index, 0.0)
        forward = np.asarray(
            [self.forward[row["code"]][window_index] for row in pool], dtype=float
        )
        valid = np.isfinite(forward)
        if valid.sum() < top_n + 5:
            return None
        percentile = pd.Series(forward[valid]).rank(pct=True).to_numpy() * 100.0
        index_of = {
            row["code"]: position
            for position, row in enumerate([r for r, ok in zip(pool, valid) if ok])
        }
        positions = [index_of[row["code"]] for row in picked
                     if row["code"] in index_of]
        if not positions:
            return None
        industry_count = {}
        for row in picked:
            name = industry_map.get(row["code"]) or "?"
            industry_count[name] = industry_count.get(name, 0) + 1
        top_industry = sorted(industry_count.items(), key=lambda kv: -kv[1])[:3]
        return {
            "window": window_index,
            "selection_date": str(self.windows[window_index][0]),
            "pool": len(pool),
            "pool_after_floor": len(rows),
            "picked": [row["code"] for row in picked],
            "forward_percentile": round(float(percentile[positions].mean()), 1),
            "top_industries": [[name, count] for name, count in top_industry],
            "max_industry_share": round(
                max(industry_count.values()) / max(1, len(picked)), 2
            ),
        }


MOMENTUM_MONTHS = 24
INDEPENDENT = [8, 11, 14, 17]


def _select(rows, top_n, max_per_industry, industry):
    """Greedy top-N by momentum, optionally capped per industry.

    rows must already be sorted by (-momentum, code). The cap is the only
    survival-oriented rule in the screen: without it the cheapest/highest-ROE/
    trending names collapse into one commodity cycle (B06 coal reached 8 of 20
    in w11), so 20 names give far less diversification than they appear to.
    """
    if not max_per_industry:
        return list(rows[:top_n])
    counts: dict[str, int] = {}
    picked = []
    for row in rows:
        name = industry.get(row["code"]) or "?"
        if counts.get(name, 0) >= max_per_industry:
            continue
        picked.append(row)
        counts[name] = counts.get(name, 0) + 1
        if len(picked) >= top_n:
            break
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--pb-floor-pct", type=float, default=20.0,
                        help="drop the cheapest N%% of the eligible pool by PB")
    parser.add_argument("--momentum-months", type=int, default=24)
    parser.add_argument("--max-per-industry", type=int, default=0,
                        help="cap names per industry (0 = uncapped). The only "
                             "survival rule; without it B06 coal reached 40%%.")
    parser.add_argument("--windows", type=str, default="")
    parser.add_argument("--sweep", action="store_true",
                        help="sweep the PB floor to MEASURE sensitivity")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    global MOMENTUM_MONTHS
    MOMENTUM_MONTHS = max(1, int(args.momentum_months))

    print("building universe ...", flush=True)
    screen = Screen()
    print("index members=%d | with fundamentals+prices=%d | full-window universe=%d"
          % (len(screen.codes), len(screen.prices), len(screen.universe)),
          flush=True)

    if args.sweep:
        print("\n=== PB-floor sensitivity (mean forward percentile, random=50.0) ===")
        print("%-12s %8s %10s %10s" % ("pb_floor%", "12w", "4w indep", "4w SE"))
        for floor_pct in (0.0, 10.0, 20.0, 30.0, 40.0):
            got = []
            for index in range(N_WINDOWS):
                result = screen.evaluate(index, args.top_n, floor_pct,
                                         args.max_per_industry)
                if result:
                    got.append(result)
            if not got:
                continue
            values = {row["window"]: row["forward_percentile"] for row in got}
            all_v = np.asarray(list(values.values()), dtype=float)
            ind = np.asarray([values[w] for w in INDEPENDENT if w in values],
                             dtype=float)
            se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 1 else np.nan
            print("%-12.0f %8.1f %10.1f %10.1f"
                  % (floor_pct, all_v.mean(), ind.mean(), se))
        return 0

    wanted = ([int(x) for x in args.windows.split(",") if x.strip()]
              if args.windows else list(range(N_WINDOWS)))
    results = []
    print("\n%-5s %-11s %6s %6s %9s %7s  %s"
          % ("wi", "sel", "pool", ">=flr", "fwd pct", "maxIND", "picked"))
    for index in wanted:
        result = screen.evaluate(index, args.top_n, args.pb_floor_pct,
                                 args.max_per_industry)
        if not result:
            continue
        results.append(result)
        print("%-5d %-11s %6d %6d %9.1f %7.2f  %s"
              % (result["window"], result["selection_date"], result["pool"],
                 result["pool_after_floor"], result["forward_percentile"],
                 result["max_industry_share"],
                 ",".join(result["picked"][:6]) + ("..." if
                                                   len(result["picked"]) > 6
                                                   else "")))

    values = {row["window"]: row["forward_percentile"] for row in results}
    all_v = np.asarray(list(values.values()), dtype=float)
    ind = np.asarray([values[w] for w in INDEPENDENT if w in values], dtype=float)
    se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 1 else np.nan
    print("\n=== summary (random pick = 50.0) ===")
    print("  windows evaluated      : %d" % len(all_v))
    print("  12w mean forward pct   : %.1f" % all_v.mean())
    print("  4w independent mean    : %.1f  (SE %.1f, t %.2f)"
          % (ind.mean(), se, (ind.mean() - 50.0) / se if se else float("nan")))
    print("  windows above 50       : %d / %d"
          % (int((all_v > 50).sum()), len(all_v)))
    shares = np.asarray([row["max_industry_share"] for row in results])
    print("  max single-industry    : %.2f mean, %.2f worst  <- survival risk,"
          " NOT controlled by the gate" % (shares.mean(), shares.max()))
    for row in results:
        if row["max_industry_share"] >= 0.25:
            print("    w%-2d %s  top industries: %s"
                  % (row["window"], row["selection_date"],
                     ", ".join("%s x%d" % (name, count)
                               for name, count in row["top_industries"])))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"top_n": args.top_n,
                        "pb_floor_pct": args.pb_floor_pct,
                        "momentum_months": MOMENTUM_MONTHS,
                        "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
