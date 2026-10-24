"""Render explicit chart data locally, preserving the raw response and failed blocks."""

import math
import re

import bleach
import markdown
import yaml

from scripts import run_company_summary as io

CHART = re.compile(r"^```chart[ \t]*\r?\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def chart(data, folder, index):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    required = ("id", "title", "unit", "scope", "period_basis", "version", "source")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(k), str) or not data[k].strip() for k in required
    ):
        raise ValueError("missing_explicit_chart_context")
    if data.get("type") not in ("line", "bar"):
        raise ValueError("unsupported_chart_type")
    x, series = data.get("x"), data.get("series")
    if (
        not isinstance(x, list)
        or not x
        or any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in x)
    ):
        raise ValueError("invalid_x_labels")
    if not isinstance(series, list) or not series:
        raise ValueError("missing_series")
    for row in series:
        values = row.get("values") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(values, list)
            or len(values) != len(x)
        ):
            raise ValueError("series_shape_mismatch")
        if any(
            v is not None and (type(v) not in (int, float) or not math.isfinite(v))
            for v in values
        ):
            raise ValueError("values_require_explicit_numeric_or_null")
    with plt.rc_context(
        {
            "font.sans-serif": ["Microsoft YaHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    ):
        fig, ax = plt.subplots(figsize=(10, 4.8), layout="constrained")
        try:
            for i, row in enumerate(series):
                values = [float("nan") if v is None else v for v in row["values"]]
                if data["type"] == "line":
                    ax.plot(range(len(x)), values, marker="o", label=row["name"])
                else:
                    width = 0.8 / len(series)
                    ax.bar(
                        [j - 0.4 + (i + 0.5) * width for j in range(len(x))],
                        values,
                        width,
                        label=row["name"],
                    )
            ax.set(
                xticks=range(len(x)),
                xticklabels=list(map(str, x)),
                title=data["title"],
                ylabel=data["unit"],
            )
            ax.legend()
            path = folder / "assets" / f"chart_{index}.png"
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as stream:
                fig.savefig(stream, format="png", dpi=150)
        finally:
            plt.close(fig)
    return path


def html(body):
    tags = (  # noqa: SIM905 - compact, readable tag vocabulary
        "h1 h2 h3 h4 p strong em ul ol li table thead tbody tr th td "
        "br pre code hr blockquote a img"
    ).split()

    def attribute(tag, name, value):
        if tag == "img":
            return name == "alt" or (
                name == "src"
                and re.fullmatch(r"assets/[A-Za-z0-9_.-]+\.png", value) is not None
            )
        return tag == "a" and name == "href"

    clean = bleach.clean(
        markdown.markdown(body, extensions=["tables", "fenced_code"]),
        tags=tags,
        attributes=attribute,
        strip=True,
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>最终报告</title>'
        '<style>body{font:16px/1.75 "Microsoft YaHei",sans-serif;max-width:1100px;margin:32px auto;padding:0 24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #d6dce5;padding:8px}th{background:#eef3fa}img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{border-left:3px solid #aaa;padding-left:14px;color:#555}</style>'
        + clean
        + "</html>"
    )


def render(folder, raw, status="complete"):
    issues, blocks = [], []

    def replace(match):
        blocks.append(match.group(1))
        index = len(blocks)
        try:
            data = yaml.safe_load(match.group(1))
            path = chart(data, folder, index)
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ArithmeticError,
            OSError,
            ImportError,
            RuntimeError,
            yaml.YAMLError,
        ) as error:
            issues.append({"chart": index, "error": str(error)})
            return (
                match.group(0) + f"\n\n> 图{index}未生成：{error}；原始图表块保留。\n"
            )
        context = "；".join(
            data[k] for k in ("unit", "scope", "period_basis", "version", "source")
        )
        return f"![图{index}]({path.relative_to(folder).as_posix()})\n\n图{index}：{data['title']}。{context}\n"

    body = CHART.sub(replace, raw)
    if status != "complete":
        body = f"> 响应状态：{status}，可能不完整；已有正文全部保留。\n\n" + body
    io.save(folder / "最终报告.md", body)
    io.save(folder / "最终报告.html", html(body))
    io.save(
        folder / "图表数据.yaml",
        yaml.safe_dump(
            {"raw_chart_blocks": blocks}, allow_unicode=True, sort_keys=False
        ),
    )
    io.save(
        folder / "呈现注记.json",
        {"status": status, "chart_blocks": len(blocks), "issues": issues},
    )
    return {"chart_blocks": len(blocks), "chart_issues": issues}
