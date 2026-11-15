from datetime import UTC, datetime
from typing import Any

from aris.modules.vega.schemas import ClusterStatus


def escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def resource_text(snapshot: ClusterStatus) -> str:
    if not snapshot.nodes:
        return "尚无节点观测数据。"
    lines = ["*Vega GPU 观测状态*"]
    states = {"busy": "观测占用", "idle": "观测空闲", "unknown": "未知"}
    for node in snapshot.nodes:
        lines.append(f"*{escape(node.node_id)}* · {node.status} · 采样 {node.telemetry_status}")
        lines.append(
            "采样："
            + (node.observed_at.isoformat() if node.observed_at else "无数据")
            + "；心跳："
            + (node.heartbeat_at.isoformat() if node.heartbeat_at else "无数据")
        )
        for gpu in node.gpus:
            observed = states[gpu.observed_state]
            if gpu.observed_state == "unknown":
                metrics = "指标未知，最后观测仅供参考"
            else:
                metrics = (
                    f"显存 {gpu.memory_used_mib}/{gpu.memory_total_mib} MiB"
                    f" · 利用率 {gpu.utilization_percent}%"
                )
            lines.append(f"GPU {gpu.index} · {escape(gpu.model)} · {observed} · {metrics}")
    footer = "以上为观测事实；预约可用性尚未评估。"
    # Slack text limits are finite; include an explicit truncation marker.
    text = "\n".join(lines)
    if len(text) > 2800:
        text = text[:2760] + "\n…更多节点请通过资源接口查询。"
    return text + "\n" + footer


def plain(value: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": value, "emoji": True}


def markdown(value: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": value, "verbatim": True}


def slack_time(value: datetime | None) -> str:
    if value is None:
        return "Not reported"
    # Slack renders the same instant in each viewer's device timezone.
    fallback = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"<!date^{int(value.timestamp())}^{{date_short_pretty}} {{time_secs}}|{fallback}>"
