"""Self-contained human-readable export of a fixed recorded-usage query."""

from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from aris.modules.lyra.usage_history import UsageHistory, usage_gpu_summary


def hours(value: int) -> str:
    amount = Decimal(value) / Decimal(3600000000)
    return f"{amount:,.3f}".rstrip("0").rstrip(".")


def daily_chart(report: UsageHistory) -> str:
    width, left, right, top, bottom, height = 760, 40, 8, 12, 26, 180
    plot = height - top - bottom
    maximum = max(1.0, *(day.recorded_microseconds / 3600000000 for day in report.days))
    slot = (width - left - right) / max(1, len(report.days))
    lines = []
    for step in range(3):
        y = top + plot * (1 - step / 2)
        label = f"{maximum * step / 2:.2f}".rstrip("0").rstrip(".")
        lines.append(f'<line x1="{left}" x2="{width - right}" y1="{y}" y2="{y}" class="axis"/>')
        lines.append(f'<text x="{left - 7}" y="{y + 3}" text-anchor="end">{label}</text>')
    stride = max(1, (len(report.days) + 7) // 8)
    for index, day in enumerate(report.days):
        bar_height = day.recorded_microseconds / 3600000000 / maximum * plot
        x, y = left + (index + 0.18) * slot, top + plot - bar_height
        label = f"{day.date.isoformat()} · {hours(day.recorded_microseconds)} GPU 小时"
        lines.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{slot * 0.64:.2f}" '
            f'height="{bar_height:.2f}" rx="2" class="bar"><title>{escape(label)}</title></rect>'
        )
        if index % stride == 0 or index == len(report.days) - 1:
            lines.append(
                f'<text x="{left + (index + 0.5) * slot:.2f}" y="{height - 5}" '
                'text-anchor="middle">'
                f"{day.date.strftime('%m-%d')}</text>"
            )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="每日已记录 GPU 小时">'
        + "".join(lines)
        + "</svg>"
    )


def history_report(report: UsageHistory, labels: dict[str, str]) -> bytes:
    zone = ZoneInfo(report.timezone)
    start = report.start_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S")
    end = report.end_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S")
    read_at = report.read_at.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S")
    subject = (
        labels.get(str(report.owner_id), "指定用户") if report.owner_id is not None else "节点汇总"
    )
    owner_rows = "".join(
        "<tr><td>"
        + escape(labels.get(str(owner.owner_id), str(owner.owner_id)))
        + f"</td><td>{hours(owner.recorded_microseconds)}</td><td>{owner.gpu_count}</td>"
        + f"<td>{owner.segment_count}</td></tr>"
        for owner in report.owners
    )
    gpu_rows = "".join(
        "<tr><td>"
        + escape(", ".join(gpu.models))
        + "<small>"
        + escape(gpu.gpu_uuid)
        + f"</small></td><td>{hours(gpu.recorded_microseconds)}</td>"
        + f"<td>{gpu.owner_count}</td><td>{gpu.segment_count}</td></tr>"
        for gpu in usage_gpu_summary(report.rows)
    )
    empty = (
        '<p class="empty">这个时段没有已记录用量。没有记录不代表 GPU 未被使用。</p>'
        if not report.rows
        else ""
    )
    template = Template((Path(__file__).with_name("static") / "report.html").read_text())
    return template.substitute(
        node=escape(report.node_id),
        subject=escape(subject),
        start=start,
        end=end,
        timezone=escape(report.timezone),
        total=hours(report.recorded_microseconds),
        gpu_count=report.gpu_count,
        segment_count=report.segment_count,
        empty=empty,
        chart=daily_chart(report),
        owner_rows=owner_rows,
        gpu_rows=gpu_rows,
        read_at=read_at,
        revision=report.snapshot.revision,
        fingerprint=escape(report.fingerprint),
    ).encode("utf-8")
