"""Render explicit distributed-share positions and settlement cash flows."""

from __future__ import annotations

import html


def render_in_kind_ledger(positions=(), cashflows=()) -> str:
    """Keep non-cash entitlements visible alongside ordinary holdings and cash."""
    if not positions and not cashflows:
        return ""

    def esc(value):
        return html.escape(str(value if value is not None else "—"), quote=True)

    rows = []
    for position in positions:
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{esc(position.get(key, '—'))}</td>"
                for key in (
                    "event_id",
                    "asset_code",
                    "quantity",
                    "fractional_quantity",
                    "delivered",
                    "value",
                    "cash_receivable",
                    "settlement_basis",
                )
            )
            + "</tr>"
        )
    flows = []
    for flow in cashflows:
        flows.append(
            "<tr>"
            + "".join(
                f"<td>{esc(flow.get(key, '—'))}</td>"
                for key in (
                    "event_id",
                    "asset_code",
                    "date",
                    "kind",
                    "amount",
                    "affects_cash",
                )
            )
            + "</tr>"
        )
    result = "<div><h3>实物分派权益与结算</h3>"
    if rows:
        result += (
            '<div class="table-wrap" style="overflow-x:auto"><table><thead><tr>'
            "<th>事件</th><th>外部标的</th><th>股数</th>"
            "<th>零股权益</th><th>已交付</th><th>账户估值</th>"
            "<th>现金应收</th><th>结算依据</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table></div>"
        )
    if flows:
        result += (
            '<div class="table-wrap" style="overflow-x:auto"><table><thead><tr>'
            "<th>事件</th><th>外部标的</th><th>日期</th>"
            "<th>现金流类型</th><th>金额</th><th>影响现金</th></tr></thead>"
            "<tbody>" + "".join(flows) + "</tbody></table></div>"
        )
    result += (
        "<p>应收权益计入净资产；交付前不可交易，结算前不作为可用现金。"
        "现金变动按账户币种记录。</p></div>"
    )
    return result
