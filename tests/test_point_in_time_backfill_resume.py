"""Offline regressions for provenance-bound market reuse and source blocking."""

from __future__ import annotations

import copy
import json
from concurrent.futures import Future
from datetime import date, timedelta

import pandas as pd
import pytest

from src.data import reference_universe_backfill as reference
from src.data.baostock_access import BaostockAccessBlocked, BaostockTransientError
from src.data.market_history import (
    CorporateAction,
    PointInTimeMarketStore,
    PriceHistoryBundle,
)
from src.data.point_in_time_backfill import PointInTimeBackfillService
from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    PointInTimeFundamentalStore,
    StatementFetchResult,
)

END = date(2026, 9, 21)
START = END - timedelta(days=int(5 * 365.25))
CODE = "600001"


def _bundle(*, start=START, actions=None, diagnostics=None):
    prices = pd.DataFrame(
        {
            "date": pd.to_datetime([start, END]),
            **{
                f"{kind}_{field}": [10.0, 12.0]
                for kind in ("raw", "qfq")
                for field in ("open", "high", "low", "close")
            },
            "qfq_factor": [1.0, 1.0],
            "volume": [100.0, 100.0],
            "tradable": [True, True],
        }
    )
    return PriceHistoryBundle(
        code=CODE,
        prices=prices,
        source="fixture",
        currency="CNY",
        actions=actions or [],
        diagnostics=diagnostics or [],
    ).validate()


def _statement(source="baostock_profit"):
    return FinancialStatementSnapshot(
        period_end=date(2025, 12, 31),
        published_at=date(2026, 4, 15),
        period_type="year",
        is_cumulative=True,
        currency="CNY",
        source=source,
        revenue=1000.0,
        net_income_parent=100.0,
    )


class Market:
    def __init__(self, outcome=None):
        self.outcome = outcome or _bundle()
        self.calls = []

    def fetch(self, *args):
        self.calls.append(args)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return copy.deepcopy(self.outcome)


class Statements:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [[]]
        self.calls = []

    def fetch(self, *args):
        self.calls.append(args)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return StatementFetchResult(statements=outcome, attempts=[])


def _config(tmp_path):
    return {
        "point_in_time_data": {
            "output_dir": str(tmp_path),
            "history_years": 5,
            "official_statement_crawlers": True,
            "market_history": {"coverage_tolerance_days": 31},
        },
        "provider_access": {
            "baostock": {"state_dir": str(tmp_path / "provider_state")}
        },
    }


def _service(tmp_path, *, market=None, statements=None, sse=None, cninfo=None):
    return PointInTimeBackfillService(
        _config(tmp_path),
        market_provider=market or Market(),
        a_share_statements=statements or Statements([_statement()]),
        sse_statements=sse or Statements(),
        cninfo_statements=cninfo or Statements(),
        hkex_statements=Statements(),
        sec_provider=Statements(),
        market_store=PointInTimeMarketStore(tmp_path),
        fundamental_store=PointInTimeFundamentalStore(tmp_path),
    )


def _manifest(codes=(CODE,)):
    return {
        "contract": reference.REFERENCE_MANIFEST_CONTRACT,
        "as_of": END.isoformat(),
        "companies": [{"code": code} for code in codes],
    }


def test_statement_only_collection_never_calls_market_history(tmp_path):
    market = Market(RuntimeError("unrelated market transport unavailable"))
    statements = Statements([_statement()])
    service = _service(tmp_path, market=market, statements=statements)
    row = service._backfill_one(CODE, START, END, include_market_history=False)
    assert market.calls == []
    assert len(statements.calls) == 1
    assert row["market_history"] == {"status": "not_requested"}
    assert row["statements"]["status"] == "success"


def test_statement_only_preserves_provider_access_block(tmp_path):
    service = _service(
        tmp_path, statements=Statements(BaostockAccessBlocked("daily_budget"))
    )
    row = service._backfill_one(CODE, START, END, include_market_history=False)
    assert row["statements"]["status"] == "blocked"
    assert row["source_access"]["blocked"]


def test_statement_retry_reuses_hashed_market_without_refetch(tmp_path):
    market = Market()
    service = _service(
        tmp_path,
        market=market,
        statements=Statements(RuntimeError("temporary"), [_statement()]),
    )
    first = service._backfill_one(CODE, START, END)
    assert first["statements"]["status"] == "failed"
    before = service._market_paths(CODE)["prices"].read_bytes()
    second = service._backfill_one(CODE, START, END)
    assert len(market.calls) == 1
    assert second["market_history"]["cache_hit"] is True
    assert second["market_history"]["cache_evidence"] == "request_receipt"
    assert second["statements"]["status"] == "success"
    assert service._market_paths(CODE)["prices"].read_bytes() == before


@pytest.mark.parametrize("matching", [False, True])
def test_reference_resume_passes_exact_checkpoint_even_with_force(tmp_path, matching):
    market = Market()
    service = _service(tmp_path, market=market)
    bundle = _bundle()
    paths = service.market_store.write(bundle)
    old_market = service._market_row(bundle, START, END, paths["prices"])
    old_market.pop(
        "action_issues"
    )  # Legacy checkpoints predate explicit action issues.
    payloads = []

    def worker(payload):
        payloads.append(payload)
        return service._backfill_one(
            payload["code"],
            date.fromisoformat(payload["start"]),
            date.fromisoformat(payload["end"]),
            previous_market=payload.get("previous_market"),
        )

    batch = reference.ReferenceUniverseBackfillService(
        _config(tmp_path), _manifest(), output_dir=tmp_path, workers=1, worker=worker
    )
    batch_id, fingerprint = batch._batch_identity(END)
    checkpoint = batch._checkpoint_payload(
        batch_id=batch_id,
        fingerprint=fingerprint,
        row={
            "code": CODE,
            "market_history": old_market,
            "statements": {"status": "failed"},
        },
    )
    if not matching:
        checkpoint["fingerprint"] = "another-batch"
    reference._atomic_json(
        tmp_path / "reference_batches" / batch_id / "codes" / f"{CODE}.json", checkpoint
    )
    report = batch.run(evaluation_date=END, force=True)
    assert report["state"] == "complete"
    if matching:
        assert payloads[0]["previous_market"] == old_market
        assert not market.calls
        assert (
            report["instruments"][0]["market_history"]["cache_evidence"]
            == "reference_checkpoint"
        )
    else:
        assert "previous_market" not in payloads[0]
        assert len(market.calls) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "prices",
        "actions_missing",
        "metadata_code",
        "request_window",
        "settings",
        "receipt_corrupt",
    ],
)
def test_corrupt_or_mismatched_receipt_refetches_without_legacy_downgrade(
    tmp_path, damage
):
    market = Market()
    service = _service(tmp_path, market=market)
    original = service._backfill_one(CODE, START, END)["market_history"]
    paths = service._market_paths(CODE)
    if damage == "prices":
        frame = pd.read_csv(paths["prices"])
        for name in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "qfq_open",
            "qfq_high",
            "qfq_low",
            "qfq_close",
        ):
            frame.loc[0, name] = 11.0
        frame.to_csv(paths["prices"], index=False)
    elif damage == "actions_missing":
        paths["actions"].unlink()
    elif damage == "metadata_code":
        data = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        data["code"] = "000002"
        paths["metadata"].write_text(json.dumps(data), encoding="utf-8")
    elif damage == "receipt_corrupt":
        paths["receipt"].write_text("[]", encoding="utf-8")
    else:
        data = json.loads(paths["receipt"].read_text(encoding="utf-8"))
        if damage == "request_window":
            data["market_history"]["requested_start"] = "2020-01-01"
        else:
            data["market_settings_hash"] = "another-config"
        paths["receipt"].write_text(json.dumps(data), encoding="utf-8")
    row = service._backfill_one(CODE, START, END, previous_market=original)
    assert len(market.calls) == 2
    assert not row["market_history"].get("cache_hit", False)


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "wrong_window",
        "wrong_path",
        "wrong_count",
        "missing_meta",
        "invalid_raw_qfq",
    ],
)
def test_legacy_cache_requires_exact_validated_original_request(tmp_path, evidence):
    market = Market()
    service = _service(tmp_path, market=market)
    bundle = _bundle()
    paths = service.market_store.write(bundle)
    row = service._market_row(bundle, START, END, paths["prices"])
    if evidence == "wrong_window":
        row["requested_end"] = "2026-09-20"
    elif evidence == "wrong_path":
        row["output"] = str(tmp_path / "different.csv")
    elif evidence == "wrong_count":
        row["rows"] = 100
    elif evidence == "missing_meta":
        paths["metadata"].unlink()
    elif evidence == "invalid_raw_qfq":
        frame = pd.read_csv(paths["prices"])
        frame.loc[0, "qfq_factor"] = 2.0
        frame.to_csv(paths["prices"], index=False)
    service._backfill_one(CODE, START, END, previous_market=row if evidence else None)
    assert len(market.calls) == 1


def test_reused_partial_history_keeps_unresolved_actions(tmp_path):
    action = CorporateAction(
        code=CODE,
        action_type="adjustment_factor_change",
        ex_date=date(2025, 1, 1),
        raw_adjustment_factor=1.2,
        source="baostock_adjust_factor",
    )
    diagnostics = ["corporate_action_fallback_unresolved:factor-only"]
    service = _service(
        tmp_path,
        market=Market(
            _bundle(start=date(2024, 1, 1), actions=[action], diagnostics=diagnostics)
        ),
    )
    first = service._backfill_one(CODE, START, END)
    second = service._backfill_one(CODE, START, END)
    assert second["market_history"]["cache_hit"] is True
    assert first["market_history"]["requested_window_coverage"] == "partial"
    assert second["market_history"]["requested_window_coverage"] == "partial"
    assert second["market_history"]["diagnostics"] == diagnostics
    assert (
        second["market_history"]["action_issues"]
        == first["market_history"]["action_issues"]
    )
    assert second["market_history"]["action_issues"]


def test_receipt_rejects_files_changed_after_snapshot_validation(tmp_path):
    service = _service(tmp_path)
    bundle = _bundle()
    paths = service.market_store.write(bundle)
    expected = service._market_row(bundle, START, END, paths["prices"])
    row, hashes = service._validated_market_snapshot(CODE, START, END, expected)
    paths["actions"].write_text("[{}]", encoding="utf-8")
    with pytest.raises(ValueError, match="changed while validating"):
        service._write_market_receipt(CODE, row, hashes)
    assert not service._market_paths(CODE)["receipt"].exists()


def test_statement_block_preserves_cached_market_and_stops_official_sources(tmp_path):
    blocked = BaostockAccessBlocked("blacklist", "access denied")
    market, official = Market(), Statements([_statement("sse_xbrl")])
    service = _service(
        tmp_path,
        market=market,
        statements=Statements(blocked),
        sse=official,
        cninfo=official,
    )
    first = service._backfill_one(CODE, START, END)
    second = service._backfill_one(CODE, START, END)
    assert first["market_history"]["status"] == "success"
    assert second["market_history"]["cache_hit"] is True
    assert second["statements"]["status"] == "blocked"
    assert second["source_access"] == blocked.to_dict()
    assert len(market.calls) == 1
    assert not official.calls


def test_market_block_stops_entire_standalone_batch(tmp_path):
    blocked = BaostockAccessBlocked(
        "daily_budget", retry_at="2026-09-23T00:00:00+08:00"
    )
    market, statements = Market(blocked), Statements([_statement()])
    service = _service(tmp_path, market=market, statements=statements)
    report = service.run([CODE, "600002"], evaluation_date=END)
    assert len(market.calls) == 1
    assert not statements.calls
    assert report["instrument_count"] == 1
    assert report["requested_instrument_count"] == 2
    assert report["pending_codes"] == ["600002"]
    assert report["source_access"] == blocked.to_dict()


def test_transient_baostock_error_still_uses_official_dated_statements(tmp_path):
    transient = BaostockTransientError("temporary transport failure")
    official = Statements([_statement("sse_xbrl")])
    service = _service(tmp_path, statements=Statements(transient), sse=official)
    row = service._backfill_one(CODE, START, END)
    assert row["statements"]["status"] == "success"
    assert row["statements"]["all_have_publication_date"] is True
    assert row["source_access"]["blocked"] is False
    assert row["statements"]["attempts"][0]["source"] == "baostock_profit"
    assert len(official.calls) == 1
    stored = service.fundamental_store.as_of(CODE, END)
    assert stored[0].source == "sse_xbrl"
    assert stored[0].published_at == date(2026, 4, 15)


def test_failed_cninfo_refresh_does_not_delete_existing_dated_reports(tmp_path):
    service = _service(
        tmp_path,
        statements=Statements(BaostockTransientError("temporary")),
        sse=Statements([_statement("sse_xbrl")]),
        cninfo=Statements(RuntimeError("official report temporarily unavailable")),
    )
    previous = _statement("cninfo_annual_report").copy(
        update={
            "period_end": date(2024, 12, 31),
            "published_at": date(2025, 4, 15),
        }
    )
    service.fundamental_store.upsert(CODE, [previous])
    row = service._backfill_one(CODE, START, END)
    assert row["statements"]["status"] == "success"
    assert any(
        item.source == "cninfo_annual_report" and item.period_end == date(2024, 12, 31)
        for item in service.fundamental_store.read_all(CODE)
    )


def test_reference_stops_after_structured_block_and_preserves_fingerprint(tmp_path):
    calls = []
    blocked = BaostockAccessBlocked("blacklist")

    def worker(payload):
        calls.append(payload["code"])
        raise blocked

    batch = reference.ReferenceUniverseBackfillService(
        _config(tmp_path),
        _manifest((CODE, "600002")),
        output_dir=tmp_path,
        workers=1,
        worker=worker,
    )
    changed = _config(tmp_path)
    changed["provider_access"]["baostock"]["daily_request_budget"] = 20000
    other = reference.ReferenceUniverseBackfillService(
        changed,
        _manifest((CODE, "600002")),
        output_dir=tmp_path,
        workers=1,
        worker=worker,
    )
    assert batch._batch_identity(END) == other._batch_identity(END)
    report = batch.run(evaluation_date=END)
    assert calls == [CODE]
    assert report["state"] == "blocked"
    assert report["source_access"] == blocked.to_dict()
    assert report["checkpoint_count"] == 1
    assert report["pending_codes"] == ["600002"]
    progress_path = (
        tmp_path / "reference_batches" / report["batch_id"] / "progress.json"
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["state"] == "blocked"
    assert progress["pending"] == 1
    assert progress["completed"] == 1


def test_parallel_block_cancels_queued_companies_without_replenishment(
    tmp_path, monkeypatch
):
    futures = []

    class Executor:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, worker, payload):
            future = Future()
            futures.append(future)
            if len(futures) == 1:
                future.set_result(
                    reference._worker_error_row(
                        payload["code"], BaostockAccessBlocked("blacklist")
                    )
                )
            return future

    monkeypatch.setattr(reference, "ProcessPoolExecutor", Executor)
    batch = reference.ReferenceUniverseBackfillService(
        _config(tmp_path),
        _manifest(tuple(f"60000{i}" for i in range(1, 7))),
        output_dir=tmp_path,
        workers=2,
    )
    report = batch.run(evaluation_date=END)
    assert report["state"] == "blocked"
    assert len(futures) == 4
    assert all(future.cancelled() for future in futures[1:])


def test_worker_propagates_block_with_legacy_three_argument_stub(monkeypatch):
    blocked = BaostockAccessBlocked("daily_budget")

    class Service:
        def __init__(self, config):
            pass

        def _backfill_one(self, code, start, end):
            raise blocked

    monkeypatch.setattr(reference, "PointInTimeBackfillService", Service)
    row = reference.run_reference_company_worker(
        {"code": CODE, "start": START.isoformat(), "end": END.isoformat(), "config": {}}
    )
    assert row["source_access"] == blocked.to_dict()
