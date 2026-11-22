"""Downloads of a weekly report assembled from saved daily totals."""

import csv
import io
from html import escape

from aris.interfaces.usage.report import hours
from aris.interfaces.usage.saved_report import COVERAGE, STATES
from aris.interfaces.usage.service import csv_text
from aris.modules.nova.coverage_schedule_reader import WeeklyCoverageReportView


def weekly_daily_csv(
    view: WeeklyCoverageReportView, labels: dict[str, str], gpu_labels: dict[str, str]
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        (
            "record_type",
            "report_id",
            "node",
            "scope",
            "week",
            "timezone",
            "coverage",
            "verified_usage_microseconds",
            "total_usage_microseconds",
            "date",
            "gpu",
            "gpu_uuid",
            "state",
            "owner_id",
            "display_name",
            "microseconds",
        )
    )
    context = (str(view.id), csv_text(view.node_id), view.scope, view.week, view.timezone)
    writer.writerow(
        (
            "summary",
            *context,
            view.coverage,
            view.verified_usage_microseconds,
            view.total_usage_microseconds,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )
    )
    for day in view.days:
        writer.writerow(("day", *context, day.coverage, "", "", day.day, "", "", "", "", "", ""))
    for row in view.durations:
        owner = str(row.owner_id or "")
        writer.writerow(
            (
                "duration",
                *context,
                "",
                "",
                "",
                "",
                csv_text(gpu_labels.get(row.gpu_uuid, row.gpu_uuid)),
                csv_text(row.gpu_uuid),
                row.state,
                owner,
                csv_text(labels.get(owner, "Unknown")) if owner else "",
                row.microseconds,
            )
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def weekly_daily_html(
    view: WeeklyCoverageReportView, labels: dict[str, str], gpu_labels: dict[str, str]
) -> bytes:
    dates = "".join(
        f"<li>{day.day.isoformat()} · {COVERAGE[day.coverage]}</li>" for day in view.days
    )
    rows = "".join(
        "<tr><td>"
        + escape(gpu_labels.get(row.gpu_uuid, row.gpu_uuid))
        + "</td><td>"
        + (escape(labels.get(str(row.owner_id), "Unknown")) if row.owner_id else "—")
        + f"</td><td>{STATES[row.state]}</td><td>{hours(row.microseconds)}</td></tr>"
        for row in view.durations
    )
    if not rows:
        rows = '<tr><td colspan="4">该视图没有可展示的用量。</td></tr>'
    total = (
        "未知" if view.total_usage_microseconds is None else hours(view.total_usage_microseconds)
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ARIS GPU 用量周报</title>"
        "<style>body{margin:2rem auto;padding:0 1rem;max-width:60rem;"
        "font:16px/1.55 system-ui;color:#17263a}table{width:100%;border-collapse:collapse;"
        "overflow-wrap:anywhere}th,td{padding:.55rem;text-align:left;"
        "border-bottom:1px solid #ced5df}"
        "ul{padding-left:1.3em}tr{break-inside:avoid}@media print{body{margin:0;padding:0}}"
        "</style></head><body><main><h1>GPU 用量周报</h1>"
        f"<p>{escape(view.node_id)} · {'本人统计' if view.scope == 'mine' else '节点统计'}</p>"
        f"<p>{view.week.isoformat()} 起 · {escape(view.timezone)} · {COVERAGE[view.coverage]}</p>"
        f"<p>已核实用量 {hours(view.verified_usage_microseconds)} GPU 小时 · 总用量 {total}</p>"
        f"<h2>每日覆盖</h2><ul>{dates}</ul>"
        "<h2>按 GPU 汇总</h2><table><thead><tr>"
        "<th>GPU</th><th>用户</th><th>状态</th><th>GPU 小时</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></main></body></html>"
    ).encode()
