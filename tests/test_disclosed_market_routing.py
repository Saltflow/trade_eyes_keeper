from datetime import date

import pytest

from src.data.market_history import MarketHistoryProvider


class Provider:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value, error, []

    def fetch(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.value


@pytest.mark.parametrize("code", ["510300", "00883", "VOO", "BRK.B"])
def test_enabled_disclosures_route_funds_and_overseas_without_yahoo(code):
    expected = object()
    disclosed = Provider(expected)
    other = Provider(error=AssertionError("wrong price basis"))
    provider = MarketHistoryProvider(
        {"point_in_time_data": {"market_history": {"disclosed_sources": True}}},
        baostock_provider=other, yahoo_provider=other,
        disclosed_provider=disclosed,
    )
    assert provider.fetch(code, date(2018, 3, 28), date(2026, 9, 23)) is expected
    assert len(disclosed.calls) == 1
    assert other.calls == []


def test_disclosure_failure_never_silently_uses_yahoo_adjusted_raw():
    error = ValueError("split or publication is unresolved")
    yahoo = Provider(object())
    provider = MarketHistoryProvider(
        {"point_in_time_data": {"market_history": {"disclosed_sources": True}}},
        baostock_provider=Provider(error=error), yahoo_provider=yahoo,
        disclosed_provider=Provider(error=error),
    )
    for code in ("002594", "510500", "00700", "VOO"):
        with pytest.raises(ValueError, match="unresolved"):
            provider.fetch(code, date(2018, 3, 28), date(2026, 9, 23))
    assert yahoo.calls == []
