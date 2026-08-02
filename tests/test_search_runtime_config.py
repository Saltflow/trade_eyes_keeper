import pytest

from src.search.config import SearchRuntimeConfig


def test_search_workers_environment_overrides_yaml(monkeypatch):
    monkeypatch.setenv("SEARCH_WORKERS", "1")

    config = SearchRuntimeConfig({"workers": 8}, genetic={})

    assert config.workers == 1


def test_search_workers_environment_rejects_zero(monkeypatch):
    monkeypatch.setenv("SEARCH_WORKERS", "0")

    with pytest.raises(ValueError, match="at least 1"):
        SearchRuntimeConfig({}, genetic={})
