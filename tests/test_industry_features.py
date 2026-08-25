from datetime import date
import json

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingSnapshot
from src.fundamental_embedding.industry import (
    INDUSTRY_CLASSIFICATION_CONTRACT,
    IndustryClassification,
    IndustryClassificationStore,
    IndustryRelativeSnapshotBuilder,
)


def _label(symbol: str, code: str) -> IndustryClassification:
    return IndustryClassification(
        symbol=symbol,
        industry_code=code,
        industry_name=code,
        taxonomy="csrc",
        effective_from=date(2026, 8, 17),
        source="test",
    )


def test_store_refuses_industry_before_its_effective_date(tmp_path):
    path = tmp_path / "industry.json"
    path.write_text(
        json.dumps(
            {
                "contract": INDUSTRY_CLASSIFICATION_CONTRACT,
                "classifications": [_label("000001", "C39").to_dict()],
            }
        ),
        encoding="utf-8",
    )
    store = IndustryClassificationStore(path)

    assert store.labels_as_of(date(2026, 8, 16)) == {}
    assert set(store.labels_as_of(date(2026, 8, 17))) == {"000001"}


def test_industry_relative_features_use_fine_then_sector_peer_groups():
    snapshot = FundamentalPricingSnapshot(
        feature_date=date(2026, 8, 18),
        symbols=("000001", "000002", "000003", "000004", "000005"),
        feature_names=("earnings_yield",),
        values=np.asarray([[1.0], [2.0], [3.0], [4.0], [5.0]]),
        availability_mask=np.ones((5, 1), dtype=bool),
    ).validate()
    labels = {
        "000001": _label("000001", "C39"),
        "000002": _label("000002", "C39"),
        "000003": _label("000003", "C39"),
        "000004": _label("000004", "C27"),
        "000005": _label("000005", "C27"),
    }

    result = IndustryRelativeSnapshotBuilder(
        minimum_industry_peers=3
    ).build(snapshot, labels)

    assert result.peer_scopes == (
        "industry",
        "industry",
        "industry",
        "sector_fallback",
        "sector_fallback",
    )
    assert result.peer_counts.tolist() == [3, 3, 3, 5, 5]
    assert result.feature_names == ("industry_relative:earnings_yield",)
    assert np.all(result.availability_mask)
    assert result.values[:, 0].tolist() == [-1.0, 0.0, 1.0, 0.5, 1.0]
    assert result.metadata["historical_walk_forward_eligible"] is False
