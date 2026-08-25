import json
from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.industry_evaluation import (
    IndustryRidgeConfig,
    build_industry_relative_dataset,
    evaluate_industry_relative_increment,
)
from src.fundamental_embedding.industry_history import (
    INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
    IndustryClassificationHistoryStore,
)


def _dataset() -> FundamentalPricingDataset:
    dates = [
        date(2021, 1, 15),
        date(2021, 4, 30),
        date(2021, 7, 30),
        date(2021, 10, 29),
        date(2022, 1, 31),
    ]
    symbols = ("000001", "000002", "000003", "000004")
    rows = [(day, symbol) for day in dates for symbol in symbols]
    values = np.asarray(
        [[index % 4, (index % 4) * 0.5 + index // 4] for index in range(len(rows))],
        dtype=float,
    )
    forward = np.asarray([0.01 * (index % 4) for index in range(len(rows))])
    return FundamentalPricingDataset(
        feature_dates=tuple(day for day, _ in rows),
        label_end_dates=tuple(day + timedelta(days=10) for day, _ in rows),
        symbols=tuple(symbol for _, symbol in rows),
        feature_names=("earnings_yield", "roe_ttm"),
        values=values,
        availability_mask=np.ones_like(values, dtype=bool),
        forward_returns=forward,
        excess_returns=forward - forward.mean(),
    )


def _history(tmp_path) -> IndustryClassificationHistoryStore:
    path = tmp_path / "history.json"
    payload = {
        "contract": INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
        "observations": [
            {
                "symbol": symbol,
                "industry_code": "C38",
                "industry_name": "电气机械",
                "taxonomy": "csrc-2012",
                "period_end": "2020-12-31",
                "published_at": "2021-01-25",
                "source_url": "https://example.test/q4.pdf",
            }
            for symbol in ("000001", "000002", "000003", "000004")
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return IndustryClassificationHistoryStore(path)


def _config() -> IndustryRidgeConfig:
    return IndustryRidgeConfig(
        maximum_label_age_days=366,
        minimum_industry_peers=2,
        minimum_date_coverage=0.5,
        minimum_train_dates=2,
        minimum_train_rows=4,
    )


def test_augmentation_uses_announced_history_not_future_taxonomy(tmp_path):
    result = build_industry_relative_dataset(_dataset(), _history(tmp_path), _config())

    early = result.coverage_by_date[0]
    later = result.coverage_by_date[1]
    assert early["published_label_count"] == 0
    assert early["peer_context_count"] == 0
    assert later["published_label_count"] == 4
    assert later["peer_context_count"] == 4
    assert result.dataset.metadata["current_taxonomy_not_used"] is True


def test_evaluation_trains_only_on_realized_labels(tmp_path):
    relative = build_industry_relative_dataset(
        _dataset(), _history(tmp_path), _config()
    )
    report = evaluate_industry_relative_increment(relative, _config())

    assert report["acceptance"]["training_labels_end_before_test_date"] is True
    assert report["quarterly_results"]
    for row in report["quarterly_results"]:
        assert row["train_date_count"] >= 2
        assert row["train_row_count"] >= 4
