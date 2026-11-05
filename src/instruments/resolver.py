"""Resolve monitor inputs against existing source adapters, never model guesses."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class SymbolResolutionError(ValueError):
    def __init__(self, query, candidates=()):
        self.candidates = list(candidates)
        detail = "、".join(f"{x['code']} ({x['name']})" for x in candidates)
        super().__init__(
            f"{query}: "
            + (
                f"请明确市场后选择证券：{detail}"
                if detail
                else "未核实到可用证券，请明确市场或稍后重试"
            )
        )


def canonical_symbol(code: str) -> str:
    value = str(code).strip().upper()
    if value.endswith("-SI"):
        return value[:-3] + ".SI"
    if value.endswith(".HK") and value[:-3].isdigit():
        return value[:-3].zfill(5)
    if value.endswith((".SS", ".SZ")) and len(value[:-3]) == 6 and value[:-3].isdigit():
        return value[:-3]
    return value


class InstrumentResolver:
    def __init__(self, config, *, quote=None, sgx=None):
        self.config = config
        self._quote = quote
        self._sgx = sgx

    def _identity(self, code):
        from .classifier import detect_market

        market = detect_market(code)
        if market == "sg":
            if self._sgx is None:
                from ..data.sgx import SGXSource
                from ..platform_composition import collection_expiry

                self._sgx = SGXSource(
                    self.config,
                    timeout=10,
                    cache_until=lambda now: collection_expiry(self.config, now),
                )
            return self._sgx.security_identity(code)
        if self._quote is None:
            from ..data.web_crawler import StockWebCrawler

            self._quote = StockWebCrawler(self.config).fetch_realtime_quote
        row = self._quote(code)
        if not row or not row.get("name") or not row.get("close"):
            return None
        if row.get("stock_code") and canonical_symbol(row["stock_code"]) != code:
            return None
        return {
            "code": code,
            "name": row["name"],
            "market": market,
            "currency": {"a_share": "CNY", "hk": "HKD", "us": "USD"}[market],
            "source": row.get("source", "qq_realtime_quote"),
        }

    def resolve(self, query: str, market: str | None = None) -> dict:
        if market not in {None, "a_share", "hk", "us", "sg"}:
            raise ValueError("市场仅支持 a_share、hk、us、sg")
        code = canonical_symbol(query)
        if not re.fullmatch(r"[A-Z0-9]{1,8}(?:\.[A-Z]{1,4})?", code):
            raise SymbolResolutionError(query)
        # Explicit unsupported suffixes must never fall through to US routing.
        if code.endswith((".T", ".JP")):
            raise ValueError("日本数据源尚未接入，不能加入监控")
        if "." in code and code.rsplit(".", 1)[1] not in {"SI", "A", "B"}:
            raise ValueError("该证券后缀没有已接入的数据源")
        if market == "sg" and "." not in code:
            code += ".SI"
        elif market == "hk" and code.isdigit():
            code = code.zfill(5)
        from .classifier import detect_market

        if market and detect_market(code) != market:
            raise ValueError("代码后缀与指定市场不一致")
        if code.isdigit() and len(code) < 5 and market != "hk":
            raise ValueError("短数字代码可能属于不同市场，请明确市场；日本尚未接入")
        candidates = [code]
        if (
            market is None
            and re.fullmatch(r"[A-Z0-9]{1,8}", code)
            and re.search(r"[A-Z]", code)
        ):
            candidates.append(code + ".SI")
        found = []
        unavailable = []
        for candidate in candidates:
            try:
                identity = self._identity(candidate)
            except Exception as exc:  # noqa: BLE001 - providers fail independently
                logger.info(
                    "Instrument lookup unavailable: %s (%s)",
                    candidate,
                    type(exc).__name__,
                )
                unavailable.append(candidate)
                continue
            if identity and identity.get("code") == candidate:
                found.append(identity)
        if len(found) != 1 or (market is None and unavailable):
            raise SymbolResolutionError(query, found)
        return found[0]
