from unittest.mock import Mock, patch

import pytest

from src.interactive.assistant.web_search import search_web


def test_public_search_returns_bounded_source_links_and_untrusted_notice():
    response = Mock(status_code=200)
    response.text = '''
      <div class="result">
        <a class="result__a" href="https://www.cninfo.com.cn/company">巨潮资讯</a>
        <a class="result__snippet">官方披露摘要</a>
      </div>
      <div class="result">
        <a class="result__a" href="javascript:alert(1)">unsafe</a>
      </div>
    '''
    with patch("src.interactive.assistant.web_search.requests.get", return_value=response) as get:
        result = search_web("600150 annual report", limit=3)
    assert len(result["results"]) == 1
    assert result["results"][0]["domain"] == "www.cninfo.com.cn"
    assert result["results"][0]["title"] == "巨潮资讯"
    assert result["results"][0]["snippet"] == "官方披露摘要"
    assert "不可信外部资料" in result["notice"]
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["timeout"] == (4, 12)


@pytest.mark.parametrize("query,limit", [("", 3), ("x" * 257, 3), ("ok", 0), ("ok", True)])
def test_public_search_rejects_invalid_requests_before_network(query, limit):
    with patch(
        "src.interactive.assistant.web_search.requests.get"
    ) as get, pytest.raises(ValueError):
        search_web(query, limit)
    get.assert_not_called()
