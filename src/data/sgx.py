"""SGX public website prices: delayed snapshots and unadjusted daily bars."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd
from zoneinfo import ZoneInfo

from ..data_hub.http import AuthorityHTTP
from ..data_hub.parsing import hash_lock
from ..data_hub.storage import atomic_write, fingerprint, load
from ..platform_composition import authority_for

BASE = "https://api.sgx.com/securities/v1.1"
ZONE = ZoneInfo("Asia/Singapore")


class SGXSource:
    """Share one market snapshot per configured collection slot across processes.

    This is a website interface without a published API quota or stability SLA.
    AuthorityHTTP enforces persistent admission and stops on access denial.
    """

    def __init__(self, config: dict, *, http=None, timeout=30, cache_until=None):
        self.config, self.timeout = config, timeout
        self.authority = authority_for(Path(__file__).resolve().parents[2], config)
        self.http = http
        self.cache_until = cache_until
        self._memo = {}

    def _read(self, url: str, field: str, *, daily=False) -> list[dict]:
        now = datetime.now(ZONE)
        until = self.cache_until(now) if self.cache_until else now
        if daily:
            until = datetime.combine(now.date() + timedelta(days=1), time(), ZONE)
        if not self.cache_until and not daily and url in self._memo:
            return self._memo[url]
        path = self.authority.materials_root / "sgx" / f"{fingerprint(url)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with hash_lock(path.with_suffix(".lock"), timeout=self.timeout):
            cached = load(path) if path.exists() else {}
            if (
                cached
                and (self.cache_until or daily)
                and now < datetime.fromisoformat(cached["valid_until"])
            ):
                if cached.get("error"):
                    raise ValueError(cached["error"])
                return cached["data"]
            record = {"url": url, "valid_until": until.isoformat()}
            client = (
                self.http
                if isinstance(self.http, AuthorityHTTP)
                else AuthorityHTTP(self.authority, self.http)
            )
            try:
                response = client.get(url, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("meta", {}).get("code")) != "200":
                    raise ValueError(f"sgx_api_error: {payload.get('meta')}")
                rows = payload.get("data", {}).get(field)
                if not isinstance(rows, list) or not rows:
                    raise ValueError("sgx_empty_or_invalid_response")
                record.update(data=rows, fetched_at=now.isoformat())
                atomic_write(path, record)
                self._memo[url] = rows
                return rows
            except Exception as exc:
                # No tight retries, including across a service restart.
                atomic_write(path, {**record, "error": str(exc)})
                raise
            finally:
                if client is not self.http:
                    client.close()

    def _security(self, code: str, *, allow_missing=False) -> dict | None:
        normalized = str(code).upper().replace("-SI", ".SI")
        if not normalized.endswith(".SI"):
            raise ValueError("SGX symbols require the .SI suffix")
        symbol = normalized[:-3]
        rows = self._read(
            BASE + "?params=nc,n,lt,o,h,l,vl,v,cur,trading_time,pv,c,p,type",
            "prices",
        )
        matches = [row for row in rows if row.get("nc") == symbol]
        if not matches and allow_missing:
            return None
        if len(matches) != 1:
            raise ValueError(f"sgx_security_not_unique_or_missing: {symbol}")
        return matches[0]

    @staticmethod
    def _row(raw: dict) -> dict:
        # The official website parses trading_time as UTC, not Singapore time.
        stamp = (
            datetime.strptime(raw["trading_time"], "%Y%m%d_%H%M%S")
            .replace(tzinfo=timezone.utc)
            .astimezone(ZONE)
        )
        result = {"date": stamp.date().isoformat(), "quote_time": stamp.isoformat()}
        for target, source in (
            ("open", "o"),
            ("close", "lt"),
            ("high", "h"),
            ("low", "l"),
            ("prev_close", "pv"),
            ("volume", "vl"),
            ("amount", "v"),
            ("change", "c"),
            ("change_pct", "p"),
        ):
            value = raw.get(source)
            result[target] = (
                float(value)
                if isinstance(value, (int, float)) and math.isfinite(value)
                else None
            )
        prices = [result[k] for k in ("open", "close", "high", "low")]
        if any(v is None or v <= 0 for v in prices) or not (
            result["low"]
            <= min(result["open"], result["close"])
            <= max(result["open"], result["close"])
            <= result["high"]
        ):
            raise ValueError("sgx_invalid_ohlc")
        if result["volume"] is not None:
            if result["volume"] < 0:
                raise ValueError("sgx_invalid_volume")
            result["volume"] *= 1000  # Official label: Volume (000s).
        result.update(
            stock_code=f"{raw['nc']}.SI",
            name=raw.get("n"),
            currency=raw.get("cur"),
            source="sgx_public_delayed",
            delay_seconds=120,
            timestamp_kind="delayed_market_snapshot",
        )
        return result

    def quote(self, code: str) -> dict:
        return self._row(self._security(code))

    def security_identity(self, code: str) -> dict | None:
        """Resolve the official directory identity, including suspended shares."""
        row = self._security(code, allow_missing=True)
        if row is None:
            return None
        return {
            "code": row["nc"] + ".SI",
            "name": row["n"],
            "market": "sg",
            "exchange": "SGX",
            "currency": row["cur"],
            "source": "sgx_public_directory",
        }

    def history(self, code: str, days: int = 120) -> pd.DataFrame:
        current = self._security(code)
        category = current["type"]
        if not str(category).isalnum():
            raise ValueError("sgx_invalid_security_category")
        rows = self._read(
            f"{BASE}/charts/historic/{category}/code/{current['nc']}/1y",
            "historic",
            daily=True,
        )
        if any(row.get("nc") != current["nc"] for row in rows):
            raise ValueError("sgx_history_security_mismatch")
        # Historic feed can lag today's snapshot; both supply real OHLC values.
        frame = pd.DataFrame([self._row(row) for row in [*rows, current]])
        frame["date"] = pd.to_datetime(frame["date"])
        start = pd.Timestamp(datetime.now(ZONE).date() - timedelta(days=days))
        frame = frame.sort_values("date", kind="stable").drop_duplicates(
            "date", keep="last"
        )
        frame = frame.loc[frame["date"] >= start].reset_index(drop=True)
        frame.attrs.update(source="sgx_public_delayed", adjustment="unadjusted")
        return frame
