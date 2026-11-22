"""Render a fixed laboratory weekly aggregate without looking up any source rows."""

import csv
import io
from html import escape

from aris.interfaces.usage.report import hours
from aris.interfaces.usage.saved_report import COVERAGE
from aris.interfaces.usage.service import csv_text
from aris.modules.nova.combined_coverage_week import CombinedCoverageWeek


def combined_week_csv(report: CombinedCoverageWeek) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "scope",
            "week",
            "timezone",
            "start_at",
            "end_at",
            "combined_fingerprint",
            "node",
            "node_report_id",
            "node_report_fingerprint",
            "coverage",
            "verified_usage_microseconds",
            "total_usage_microseconds",
            "verified_retained_gap_microseconds",
        )
    )
    context = (
        report.week.isoformat(),
        report.timezone,
        report.start_at.isoformat(),
        report.end_at.isoformat(),
        report.fingerprint,
    )
    writer.writerow(
        (
            "lab",
            *context,
            "",
            "",
            "",
            report.coverage,
            report.verified_usage_microseconds,
            report.total_usage_microseconds,
            report.verified_retained_gap_microseconds,
        )
    )
    for node in report.nodes:
        writer.writerow(
            (
                "node",
                *context,
                csv_text(node.node_id),
                node.report_id,
                node.report_fingerprint,
                node.coverage,
                node.verified_usage_microseconds,
                node.verified_usage_microseconds if node.coverage == "complete" else None,
                node.verified_retained_gap_microseconds,
            )
        )
    return "\ufeff" + output.getvalue()


def _amount(value: int | None) -> str:
    return hours(value) if value is not None else "未知"


def combined_week_html(report: CombinedCoverageWeek) -> str:
    rows = "".join(
        f'<tr><th scope="row">{escape(node.node_id)}</th>'
        f"<td>{'缺少周报' if node.coverage == 'missing' else COVERAGE[node.coverage]}</td>"
        f"<td>{_amount(node.verified_usage_microseconds)}</td>"
        f"<td>{_amount(node.verified_retained_gap_microseconds)}</td></tr>"
        for node in report.nodes
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ARIS 实验室 GPU 用量周报</title>"
        "<style>body{margin:2rem auto;padding:0 1rem;max-width:60rem;color:#17263a;"
        "font:16px/1.55 system-ui}table{width:100%;border-collapse:collapse}"
        "td,th{padding:.55rem;text-align:left;border-bottom:1px solid #ced5df}"
        "tr{break-inside:avoid}small,td,th{overflow-wrap:anywhere}"
        "@media print{body{margin:0;padding:0}}</style></head><body><main>"
        f"<h1>实验室 GPU 用量周报</h1><p>{report.week.isoformat()} 起 · "
        f"{escape(report.timezone)} · {COVERAGE[report.coverage]}</p>"
        f"<p>已核实用量 {_amount(report.verified_usage_microseconds)} GPU 小时 · "
        f"总用量 {_amount(report.total_usage_microseconds)}</p>"
        "<table><thead><tr><th>节点</th><th>覆盖</th><th>已核实 GPU 小时</th>"
        f"<th>核实短空档（GPU 小时）</th></tr></thead><tbody>{rows}</tbody></table>"
        f"<p><small>报告指纹 {report.fingerprint}</small></p></main></body></html>"
    )
