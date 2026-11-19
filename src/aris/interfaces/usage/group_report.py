"""Standalone, explicitly recorded-only exports retain every selected node, including empty ones."""

import csv
import io
from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from aris.interfaces.usage.report import daily_chart, hours
from aris.interfaces.usage.service import csv_text
from aris.modules.lyra.group_usage import GroupUsageReport


def group_summary_csv(report: GroupUsageReport, labels: dict[str, str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "node",
            "view",
            "owner_id",
            "owner_display_name",
            "start_at",
            "end_at",
            "timezone",
            "recorded_gpu_hours",
            "recorded_microseconds",
            "gpu_count",
            "segment_count",
            "ledger_revision",
            "ledger_fingerprint",
            "group_fingerprint",
            "source_coverage",
            "total_usage_microseconds",
            "quota_usage_available",
        )
    )
    for node in report.histories:
        writer.writerow(
            (
                csv_text(node.node_id),
                "mine" if report.owner_id is not None else "node",
                str(report.owner_id) if report.owner_id is not None else "",
                csv_text(labels.get(str(report.owner_id), ""))
                if report.owner_id is not None
                else "",
                node.start_at.isoformat(),
                node.end_at.isoformat(),
                node.timezone,
                str(
                    (Decimal(node.recorded_microseconds) / Decimal(3600000000)).quantize(
                        Decimal("0.000000001")
                    )
                ),
                node.recorded_microseconds,
                node.gpu_count,
                node.segment_count,
                node.snapshot.revision,
                node.snapshot.fingerprint or "",
                report.fingerprint,
                "unknown",
                "",
                "false",
            )
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def group_html_report(report: GroupUsageReport, labels: dict[str, str]) -> bytes:
    query = report.query
    zone = ZoneInfo(query.timezone)
    nodes = "".join(
        "<tr><td>"
        + escape(node.node_id)
        + "</td><td>"
        + hours(node.recorded_microseconds)
        + f"</td><td>{node.gpu_count}</td><td>{node.segment_count}</td>"
        + f"<td>{node.snapshot.revision}</td></tr>"
        for node in report.histories
    )
    owners = "".join(
        "<tr><td>"
        + escape(labels.get(str(owner.owner_id), "Unknown"))
        + "</td><td>"
        + hours(owner.recorded_microseconds)
        + f"</td><td>{owner.node_count}</td><td>{owner.segment_count}</td></tr>"
        for owner in report.owners
    )
    evidence = "".join(
        "<li>"
        + escape(node.node_id)
        + f" · ledger {node.snapshot.revision}<small>"
        + escape(node.snapshot.fingerprint or "Empty recorded prefix")
        + "</small></li>"
        for node in report.histories
    )
    template = Template((Path(__file__).with_name("static") / "group-report.html").read_text())
    return template.substitute(
        node_count=len(report.histories),
        subject=escape(labels.get(str(report.owner_id), "本人") if report.owner_id else "节点汇总"),
        start=query.start_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S"),
        end=query.end_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S"),
        timezone=escape(query.timezone),
        recorded=hours(report.recorded_microseconds),
        segments=report.segment_count,
        chart=daily_chart(report.days),
        nodes=nodes,
        owners=owners or '<tr><td colspan="4">这个时段没有已记录用量，不代表未使用。</td></tr>',
        evidence=evidence,
        generated=report.read_finished_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S"),
        fingerprint=report.fingerprint,
    ).encode("utf-8")
