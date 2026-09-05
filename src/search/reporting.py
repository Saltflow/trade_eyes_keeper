"""Responsive, window-aware HTML reports for optimizer artifacts."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Mapping


def _esc(value: object) -> str:
    return html.escape("—" if value in (None, "") else str(value), quote=True)


def _num(value: object, suffix: str = "%", digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _role_label(role: object) -> str:
    return {
        "ranking": "Ranking",
        "purged": "Purged",
        "holdout": "Holdout",
    }.get(str(role), str(role or "Window"))


def _window_rows(data: Mapping[str, object]) -> list[dict]:
    rows: list[dict] = []
    for key, fallback_role in (
        ("ranking_windows", "ranking"),
        ("purged_windows", "purged"),
        ("isolated_windows", "purged"),
        ("holdout_windows", "holdout"),
    ):
        if key == "isolated_windows" and data.get("purged_windows") is not None:
            continue
        values = data.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("role", fallback_role)
                rows.append(row)
    rows.sort(key=lambda row: int(row.get("global_index", 0) or 0))
    for index, row in enumerate(rows, 1):
        row.setdefault("global_index", index)
    return rows


def render_optimizer_report(data: Mapping[str, object]) -> str:
    """Render a complete artifact report without external JS or CDN assets."""
    search = data.get("search", {})
    search = search if isinstance(search, dict) else {}
    contract = search.get("time_contract", {})
    contract = contract if isinstance(contract, dict) else {}
    holdout = data.get("holdout_summary", {})
    holdout = holdout if isinstance(holdout, dict) else {}
    rows = _window_rows(data)
    counts = (
        len(rows),
        int(search.get("ranking_window_count", 0) or 0),
        int(search.get("purged_overlap_window_count", 0) or 0),
        int(search.get("validation_window_count", 0) or 0),
    )
    params = data.get("params", {})
    params = params if isinstance(params, dict) else {}
    benchmarks = data.get("control_benchmarks", [])
    benchmarks = benchmarks if isinstance(benchmarks, list) else []
    activation = data.get("activation", {})
    activation = activation if isinstance(activation, dict) else {}
    market_contract = data.get("market_contract", {})
    market_contract = market_contract if isinstance(market_contract, dict) else {}
    readiness = data.get("data_readiness", {})
    readiness = readiness if isinstance(readiness, dict) else {}
    readiness_issues = readiness.get("issues", [])
    readiness_issues = readiness_issues if isinstance(readiness_issues, list) else []

    window_html = []
    for row in rows:
        period = row.get("period", {})
        period = period if isinstance(period, dict) else {}
        window_html.append(
            "<tr>"
            f"<td>{_esc(row.get('global_index'))}</td>"
            f"<td><span class='role role-{_esc(row.get('role'))}'>"
            f"{_esc(_role_label(row.get('role')))} "
            f"#{_esc(row.get('role_index'))}</span></td>"
            f"<td>{_esc(period.get('train_start'))}<br>→ {_esc(period.get('train_end'))}</td>"
            f"<td>{_esc(period.get('test_start'))}<br>→ {_esc(period.get('test_end'))}</td>"
            f"<td>{_esc(_num(row.get('return')))}</td>"
            f"<td>{_esc(_num(row.get('excess_return')))}</td>"
            f"<td>{_esc(_num(row.get('max_drawdown')))}</td>"
            f"<td>{_esc(_num(row.get('sharpe_ratio'), '', 3))}</td>"
            f"<td>{_esc(row.get('trade_count', '—'))}</td>"
            "</tr>"
        )
    if not window_html:
        window_html.append(
            "<tr><td colspan='9' class='empty'>暂无窗口明细；旧 artifact 可能未保存窗口序列。</td></tr>"
        )

    holdout_cards = [
        ("整体收益", _num(holdout.get("return_pct"))),
        ("整体超额", _num(holdout.get("excess_return_pct"))),
        ("最差最大回撤", _num(holdout.get("max_drawdown_pct"))),
        ("整体 Sharpe", _num(holdout.get("sharpe_ratio"), "", 3)),
    ]
    holdout_html = "".join(
        f"<div class='metric'><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>"
        for label, value in holdout_cards
    )
    param_html = "".join(
        f"<span class='pill'>{_esc(key)}={_esc(value)}</span>"
        for key, value in params.items()
    ) or "<span class='muted'>—</span>"
    benchmark_html = " · ".join(_esc(item) for item in benchmarks) or "—"
    passed = "通过" if activation.get("eligible") else "未通过/待人工确认"
    title = f"{data.get('group', '')} · {data.get('strategy_id', '')} · {data.get('timestamp', '')}"
    readiness_detail = (
        "全部输入已就绪"
        if not readiness_issues
        else "；".join(
            f"{item.get('code', '—')}: {item.get('reason', '未就绪')}"
            for item in readiness_issues[:20]
            if isinstance(item, dict)
        )
    )
    holdout_test_months = contract.get(
        "holdout_test_months",
        contract.get("holdout_months", contract.get("holdout_window_months")),
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
body{{margin:0;background:#eef3f8;color:#21364b;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;overflow-wrap:anywhere}}
.page{{max-width:1180px;margin:0 auto;padding:16px 12px 32px}}
.hero,.card{{background:#fff;border:1px solid #dbe5ef;border-radius:12px;box-shadow:0 2px 8px #173b6210;padding:16px;margin-bottom:12px}}
h1{{margin:0;color:#173b62;font-size:22px}} h2{{margin:0 0 10px;color:#173b62;font-size:17px}}
.muted{{color:#718096}} .meta{{color:#60748a;margin-top:5px;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}}
.metric{{padding:10px;border-radius:8px;background:#f6f9fc;border:1px solid #e2ebf3;min-width:0}}
.metric span{{display:block;color:#718096;font-size:12px}} .metric strong{{display:block;margin-top:3px;font-size:18px;color:#173b62}}
.metric strong:first-letter{{color:#173b62}} .partition{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}
.partition .metric{{flex:1 1 150px}} .pill,.role{{display:inline-block;padding:3px 7px;margin:2px;border-radius:999px;background:#edf5fc;color:#285d8d;font-size:12px}}
.role-ranking{{background:#eaf3fb;color:#21608e}} .role-purged{{background:#fff5df;color:#8a6316}} .role-holdout{{background:#eaf7ef;color:#35734d}}
.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}} table{{width:100%;min-width:880px;border-collapse:collapse;font-size:12px}}
th,td{{border-bottom:1px solid #e5edf4;padding:8px;text-align:left;vertical-align:top}} th{{background:#f2f6fa;color:#526b86;white-space:nowrap}}
.empty{{padding:18px;text-align:center;color:#718096}} .contract{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}}
.contract div{{padding:7px 9px;background:#f7fafc;border-radius:7px;color:#526b86;font-size:12px}}
@media(max-width:640px){{.page{{padding:10px 8px 24px}}.hero,.card{{padding:12px;border-radius:9px}}h1{{font-size:19px}}.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.contract{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main class="page">
<section class="hero"><h1>{_esc(title)}</h1><div class="meta">Solver {_esc(data.get('solver_id'))} · 参数 schema {_esc(data.get('parameter_schema'))} · 激活状态 {_esc(passed)}</div></section>
<section class="card"><h2>市场独立合同</h2><div class="contract">
<div>市场：{_esc(data.get('group'))}</div><div>策略：{_esc(data.get('strategy_id'))}</div><div>Solver：{_esc(data.get('solver_id'))}</div>
<div>Gate：{_esc(data.get('gate_profile'))}</div><div>Walk-Forward：{_esc(market_contract.get('walk_forward_profile'))}</div><div>Execution：{_esc(market_contract.get('execution_profile'))}</div>
<div>Benchmark：{_esc(market_contract.get('benchmark_profile'))}</div><div>配置指纹：{_esc(data.get('market_config_hash'))}</div><div>Gate 条件指纹：{_esc(search.get('gate_profile_hash'))}</div>
</div></section>
<section class="card"><h2>窗口合同总览</h2><div class="grid">
<div class="metric"><span>总窗口</span><strong>{counts[0]}</strong></div><div class="metric"><span>Ranking</span><strong>{counts[1]}</strong></div><div class="metric"><span>Purged</span><strong>{counts[2]}</strong></div><div class="metric"><span>Holdout</span><strong>{counts[3]}</strong></div>
</div><div class="meta">合同：{_esc(contract.get('total_months'))} 个月历史 · 状态预热 {_esc(contract.get('state_lookback_months'))} 个月 · Holdout {_esc(contract.get('holdout_window_count'))} × {_esc(holdout_test_months)} 个月窗口（总跨度 {_esc(contract.get('holdout_window_months'))} 个月） · 重叠窗口不复合计算</div></section>
<section class="card"><h2>Holdout 整体指标</h2><div class="grid">{holdout_html}</div><div class="meta">4 个重叠窗口采用等权平均收益、超额和 Sharpe；最大回撤取最差窗口。</div></section>
<section class="card"><h2>全部窗口明细</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>角色</th><th>训练区间</th><th>测试区间</th><th>收益</th><th>超额</th><th>最大回撤</th><th>Sharpe</th><th>交易</th></tr></thead><tbody>{''.join(window_html)}</tbody></table></div></section>
<section class="card"><h2>参数与数据合同</h2><div>{param_html}</div><div class="meta">基准：{benchmark_html}</div><div class="contract"><div>运行时间：{_esc(data.get('timestamp'))}</div><div>WF 得分：{_esc(data.get('wf_score'))}</div><div>数据合同：{_esc(data.get('contracts', {}).get('data_contract_hash') if isinstance(data.get('contracts'), dict) else '—')}</div></div><div class="meta">数据就绪：{_esc(readiness_detail)}</div></section>
</main></body></html>"""


def write_optimizer_report(data: Mapping[str, object], output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_optimizer_report(data), encoding="utf-8")
    return path
