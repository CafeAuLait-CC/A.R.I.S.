"""Self-contained exports of one authorized saved snapshot, including unknown coverage."""

import csv
import io
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from aris.interfaces.usage.report import hours
from aris.interfaces.usage.service import csv_text
from aris.modules.lyra.window_reader_schemas import SavedWindowView

COVERAGE = {"complete": "完整", "partial": "部分覆盖", "unknown": "未知"}
STATES = {"recorded": "已记录", "observed_idle": "空闲", "unknown": "未知", "uncovered": "未覆盖"}


def saved_csv(view: SavedWindowView, labels: dict[str, str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        (
            "record_type",
            "snapshot_id",
            "snapshot_fingerprint",
            "projection",
            "view_fingerprint",
            "recorded_fingerprint",
            "node",
            "detail_scope",
            "query_owner_id",
            "timezone",
            "window_start_utc",
            "window_end_utc",
            "captured_utc",
            "source_fingerprint",
            "coverage",
            "source_prefix_complete",
            "recorded_microseconds",
            "verified_usage_microseconds",
            "total_usage_microseconds",
            "covered_gpu_microseconds",
            "unknown_gpu_microseconds",
            "uncovered_gpu_microseconds",
            "gpu_uuid",
            "state",
            "owner_id",
            "display_name",
            "start_utc",
            "end_utc",
            "duration_microseconds",
            "current_quota_authorization",
        )
    )
    context = (
        str(view.id),
        view.snapshot_fingerprint,
        view.projection,
        view.view_fingerprint or "",
        view.recorded_fingerprint or "",
        csv_text(view.query.node_id),
        view.detail_scope,
        str(view.query.owner_id or ""),
        view.query.timezone,
        view.query.start_at.isoformat(),
        view.query.end_at.isoformat(),
        view.captured_at.isoformat(),
        view.source_fingerprint,
        view.coverage,
        str(view.source_prefix_complete).lower(),
    )
    totals = (
        view.recorded_microseconds,
        view.verified_usage_microseconds,
        view.total_usage_microseconds if view.total_usage_microseconds is not None else "",
        view.covered_gpu_microseconds,
        view.unknown_gpu_microseconds,
        view.uncovered_gpu_microseconds,
    )
    writer.writerow(("summary", *context, *totals, *("" for _ in range(7)), "false"))
    for cell in view.cells:
        writer.writerow(
            (
                "interval",
                *context,
                *("" for _ in totals),
                csv_text(cell.gpu_uuid),
                cell.state,
                str(cell.owner_id or ""),
                csv_text(labels.get(str(cell.owner_id), "Unknown")) if cell.owner_id else "",
                cell.start_at.isoformat(),
                cell.end_at.isoformat(),
                (cell.end_at - cell.start_at) // timedelta(microseconds=1),
                "false",
            )
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def saved_report(
    view: SavedWindowView,
    labels: dict[str, str],
    gpu_indices: dict[str, int] | None = None,
) -> bytes:
    zone = ZoneInfo(view.query.timezone)

    def stamp(value: datetime) -> str:
        return value.astimezone(zone).strftime("%Y/%m/%d %H:%M:%S")

    rows = "".join(
        "<tr><td>"
        + (
            f"GPU {gpu_indices[cell.gpu_uuid]}"
            if gpu_indices is not None and cell.gpu_uuid in gpu_indices
            else escape(cell.gpu_uuid)
        )
        + "</td><td>"
        + escape(STATES[cell.state])
        + (
            "</td><td>" + escape(labels.get(str(cell.owner_id), "Unknown"))
            if cell.owner_id
            else "</td><td>—"
        )
        + "</td><td>"
        + stamp(cell.start_at)
        + "</td><td>"
        + stamp(cell.end_at)
        + "</td><td>"
        + hours((cell.end_at - cell.start_at) // timedelta(microseconds=1))
        + "</td></tr>"
        for cell in view.cells
    )
    if not rows:
        rows = (
            '<tr><td colspan="6" class="empty">当前视图没有可展示的区间；'
            "完整性以快照的覆盖状态为准。</td></tr>"
        )
    template = Template((Path(__file__).with_name("static") / "saved-report.html").read_text())
    return template.substitute(
        node=escape(view.query.node_id),
        subject="本人统计" if view.detail_scope == "mine" else "节点统计",
        start=stamp(view.query.start_at),
        end=stamp(view.query.end_at),
        timezone=escape(view.query.timezone),
        coverage=COVERAGE[view.coverage],
        tone=view.coverage,
        recorded=hours(view.recorded_microseconds),
        verified=hours(view.verified_usage_microseconds),
        total="未知"
        if view.total_usage_microseconds is None
        else hours(view.total_usage_microseconds),
        covered=hours(view.covered_gpu_microseconds),
        unknown=hours(view.unknown_gpu_microseconds),
        uncovered=hours(view.uncovered_gpu_microseconds),
        prefix="来源段完整" if view.source_prefix_complete else "来源段尚未齐全",
        rows=rows,
        captured=stamp(view.captured_at),
        created=stamp(view.created_at),
        fingerprint=view.view_fingerprint or view.snapshot_fingerprint,
        scope_note=("个人投影 · 原节点快照依据 " + view.snapshot_fingerprint)
        if view.projection == "personal_from_node"
        else "原统计快照",
    ).encode("utf-8")
