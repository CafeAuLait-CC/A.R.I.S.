from datetime import UTC, datetime
from typing import Any

from aris.modules.vega.schemas import ClusterStatus, GpuStatus


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
        return "无数据"
    # Slack renders the same instant in each viewer's device timezone.
    fallback = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"<!date^{int(value.timestamp())}^{{date_short_pretty}} {{time_secs}}|{fallback}>"


def meter(percent: float) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def gpu_block(gpu: GpuStatus) -> dict[str, Any]:
    title = f"GPU {gpu.index} · {gpu.model}"
    if gpu.observed_state == "unknown":
        reason = "设备不在最新清单中" if not gpu.present else "缺少当前有效观测"
        return {"type": "section", "text": plain(title + "\n⚪ 指标未知 · " + reason)}
    state = "🟠 观测占用" if gpu.observed_state == "busy" else "⚪ 观测空闲"
    temperature = "温度未知" if gpu.temperature_c is None else f"{gpu.temperature_c} °C"
    memory_percent = 100 * gpu.memory_used_mib / gpu.memory_total_mib
    return {
        "type": "section",
        "text": plain(title + "\n" + state + " · " + temperature),
        "fields": [
            markdown(
                f"*GPU 利用率 · {gpu.utilization_percent}%*\n{meter(gpu.utilization_percent)}"
            ),
            markdown(
                f"*显存 · {memory_percent:.0f}%*\n{meter(memory_percent)}\n"
                f"{gpu.memory_used_mib / 1024:.1f} / {gpu.memory_total_mib / 1024:.1f} GiB"
            ),
        ],
    }


def home_view(snapshot: ClusterStatus) -> dict[str, Any]:
    gpus = [gpu for node in snapshot.nodes for gpu in node.gpus]
    online = sum(node.status == "ONLINE" for node in snapshot.nodes)
    counts = {
        state: sum(gpu.observed_state == state for gpu in gpus)
        for state in ("idle", "busy", "unknown")
    }
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": plain("Vega · GPU 状态")},
        {
            "type": "section",
            "fields": [
                markdown(f"*节点在线*\n{online} / {len(snapshot.nodes)}"),
                markdown(
                    f"*GPU 观测*\n{counts['idle']} 空闲 · {counts['busy']} 占用 · "
                    f"{counts['unknown']} 未知"
                ),
            ],
        },
        {
            "type": "context",
            "elements": [markdown("快照生成于 " + slack_time(snapshot.generated_at))],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "refresh_home",
                    "text": plain("刷新状态"),
                    "style": "primary",
                }
            ],
        },
        {"type": "divider"},
    ]
    if not snapshot.nodes:
        blocks.append({"type": "section", "text": plain("尚无节点观测数据。")})
    truncated = False
    health = {"ONLINE": "🟢 在线", "STALE": "🟡 心跳陈旧", "OFFLINE": "🔴 离线"}
    freshness = {
        "CURRENT": "采样新鲜",
        "STALE": "采样陈旧",
        "MISSING": "尚无采样",
        "ERROR": "采样异常",
    }
    for node in snapshot.nodes:
        # Home permits 100 blocks. Reserve room for at least one device and
        # explicit truncation/fact notices; do not silently hide the tail.
        if len(blocks) + 5 > 97:
            truncated = True
            break
        blocks.extend(
            [
                {"type": "header", "text": plain(node.node_id)},
                {
                    "type": "context",
                    "elements": [
                        plain(f"{health[node.status]} · {freshness[node.telemetry_status]}")
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                        markdown("采样 " + slack_time(node.observed_at)),
                        markdown("心跳 " + slack_time(node.heartbeat_at)),
                    ],
                },
            ]
        )
        if not node.gpus:
            blocks.append({"type": "section", "text": plain("尚无 GPU 清单。")})
        for gpu in node.gpus:
            if len(blocks) >= 97:
                truncated = True
                break
            blocks.append(gpu_block(gpu))
        if truncated:
            break
        blocks.append({"type": "divider"})
    if truncated:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    plain("页面已达到展示上限，部分节点或 GPU 未显示；完整清单请查看 WebUI。")
                ],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                plain("打开 Home 或点击刷新后获取快照。以上为观测事实；预约可用性尚未评估。")
            ],
        }
    )
    return {"type": "home", "blocks": blocks}
