import json
from datetime import date

from src.fundamental_embedding.industry_history import (
    INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
    IndustryClassificationHistoryStore,
)


def _history(tmp_path):
    path = tmp_path / "industry_history.json"
    payload = {
        "contract": INDUSTRY_CLASSIFICATION_HISTORY_CONTRACT,
        "observations": [
            {
                "symbol": "000001",
                "industry_code": "C38",
                "industry_name": "电气机械",
                "taxonomy": "csrc-2012",
                "period_end": "2020-12-31",
                "published_at": "2021-01-25",
                "source_url": "https://example.test/2020q4.pdf",
            },
            {
                "symbol": "000001",
                "industry_code": "C39",
                "industry_name": "计算机",
                "taxonomy": "csrc-2012",
                "period_end": "2021-03-31",
                "published_at": "2021-04-14",
                "source_url": "https://example.test/2021q1.pdf",
            },
            {
                "symbol": "000002",
                "industry_code": "B06",
                "industry_name": "煤炭",
                "taxonomy": "csrc-2012",
                "period_end": "2020-12-31",
                "published_at": "2021-01-25",
                "source_url": "https://example.test/2020q4.pdf",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return IndustryClassificationHistoryStore(path)


def test_history_uses_publication_date_not_classification_period(tmp_path):
    store = _history(tmp_path)

    assert store.labels_as_of(date(2021, 1, 24)) == {}
    q4_labels = store.labels_as_of(date(2021, 2, 1))
    assert q4_labels["000001"].industry_code == "C38"
    q1_labels = store.labels_as_of(date(2021, 4, 14))
    assert q1_labels["000001"].industry_code == "C39"


def test_history_reports_symbol_coverage_at_each_feature_date(tmp_path):
    store = _history(tmp_path)

    incomplete = store.coverage_as_of(date(2021, 1, 1), ["000001", "000002"])
    complete = store.coverage_as_of(date(2021, 1, 25), ["000001", "000002"])
    assert incomplete["historical_walk_forward_eligible"] is False
    assert incomplete["missing_symbols"] == ["000001", "000002"]
    assert complete["historical_walk_forward_eligible"] is True
