"""Load explicitly identified Ke return artifacts without old-strategy fallback."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from ..markets import _detect_fine_group

CURVE_CONTRACT = "ke-return-curves/1"
STRATEGY_ID = "ke_threshold_allocation"
MARKETS = ("a_share", "hk", "us")


def configured_universe(config: dict) -> list[str]:
    result = []
    for item in config.get("stocks", []):
        code = (
            item.get("stock_code", item.get("code", ""))
            if isinstance(item, dict)
            else item
        )
        code = str(code).strip()
        if code and code not in result:
            result.append(code)
    return result


def unavailable_curves(reason: str) -> dict:
    return {
        "contract": CURVE_CONTRACT,
        "strategy_id": STRATEGY_ID,
        "markets": {
            group: {"coverage": {"status": "unavailable", "message": reason}}
            for group in MARKETS
        },
    }


def _validate_market(item: dict, current: date, max_age: int) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("coverage"), dict):
        raise TypeError("Ke曲线数据格式错误")
    status = item["coverage"].get("status")
    if status not in ("complete", "partial", "unavailable"):
        raise ValueError("Ke曲线覆盖状态无效")
    if status == "unavailable":
        return
    if not isinstance(item.get("dates"), list) or not isinstance(
        item.get("values"), list
    ):
        raise TypeError("Ke曲线日期或净值格式错误")
    days = [date.fromisoformat(stamp) for stamp in item["dates"]]
    values = item["values"]
    capital = float(item["initial_capital"])
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("Ke曲线初始资金无效")
    if len(days) != len(values) or not 2 <= len(days) <= 10000:
        raise ValueError("Ke曲线日期与净值数量不符或不足")
    if any(a >= b for a, b in zip(days, days[1:])) or days[-1] > current:
        raise ValueError("Ke曲线日期顺序错误或包含未来日期")
    if (current - days[-1]).days > max_age:
        raise ValueError(f"Ke曲线行情截至{days[-1]}，已超过{max_age}天有效期")
    if any(
        isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0
        for v in values
    ):
        raise ValueError("Ke曲线净值无效")
    if item.get("data_as_of") != days[-1].isoformat():
        raise ValueError("Ke曲线截止日期与末次行情不一致")


def load_return_curves(config: dict, *, today: date | None = None) -> dict:
    """Validate shared provenance once, then each market independently.

    This reads local completed simulations only. It never promotes a strategy,
    scans orders, recalculates old technical strategies or mutates an account.
    """
    path = (config.get("report", {}) or {}).get("return_curve_artifact")
    if not path:
        return unavailable_curves("Ke收益曲线尚未生成")
    current = today or datetime.now(timezone.utc).date()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except TypeError:
        return unavailable_curves("Ke曲线路径配置无效")
    except OSError:
        return unavailable_curves("Ke曲线文件无法读取")
    except (ValueError, UnicodeError):
        return unavailable_curves("Ke曲线文件格式错误")
    try:
        if not isinstance(payload, dict):
            raise TypeError("Ke曲线文件格式错误")
        if (
            payload.get("contract") != CURVE_CONTRACT
            or payload.get("strategy_id") != STRATEGY_ID
        ):
            raise ValueError("Ke曲线合同或策略不匹配")
        if not isinstance(payload.get("universe"), list) or any(
            not isinstance(code, str) or not code.strip()
            for code in payload["universe"]
        ):
            raise TypeError("Ke曲线标的清单格式错误")
        generated = datetime.fromisoformat(payload["generated_at"])
        if generated.tzinfo is None or generated.date() > current:
            raise ValueError("Ke曲线生成时间无时区或晚于报告日期")
        max_age = int(
            (config.get("report", {}) or {}).get("return_curve_max_age_days", 7)
        )
        if max_age < 0:
            raise ValueError("Ke曲线有效期配置无效")
        if (current - generated.date()).days > max_age:
            raise ValueError(f"Ke曲线生成于{generated.date()}，已超过{max_age}天有效期")
        markets = payload["markets"]
        if not isinstance(markets, dict):
            raise TypeError("Ke曲线市场目录格式错误")
    except (ValueError, TypeError) as exc:
        return unavailable_curves(str(exc))
    except (KeyError, OverflowError):
        return unavailable_curves("Ke曲线元数据缺失或格式错误")

    expected = set(configured_universe(config))
    recorded = set(map(str, payload["universe"]))
    validated = {}
    for group in MARKETS:
        try:
            if {c for c in expected if _detect_fine_group(c) == group} != {
                c for c in recorded if _detect_fine_group(c) == group
            }:
                raise ValueError("该市场监控标的已变更，需重新生成Ke曲线")
            if group not in markets:
                raise ValueError("缺少该市场Ke曲线")
            item = markets[group]
            _validate_market(item, current, max_age)
            validated[group] = item
        except (ValueError, TypeError) as exc:
            validated[group] = {
                "coverage": {"status": "unavailable", "message": str(exc)}
            }
        except (KeyError, OverflowError):
            validated[group] = {
                "coverage": {
                    "status": "unavailable",
                    "message": "Ke曲线数据缺失或格式错误",
                }
            }
    return {**payload, "markets": validated}
